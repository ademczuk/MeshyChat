# v1 design rules, derived from what actually went wrong

Mined 2026-09-21 from 253 recorded issues (17 open), ~145 local lesson files, the shared
knowledge graph, and the project CHANGELOG. **Every rule below names the incident that
motivates it.** A rule with no incident behind it does not belong in this file.

The theme across all of it: *the defects were not language defects.* They were places where a
convention was trusted to hold something a mechanism should have held. Django makes some of these
easier to get right (one ORM chokepoint) and at least one notably easier to get WRONG (a
per-request aggregate hidden behind `.count()`).

---

## A. Must be settled BEFORE the first release (they are schema or architecture)

**A1. Retention must never be able to delete audit history.**
`prune_invocation_frames` deletes whole chains keyed by `chain_id`, which is the
long-lived ROOT GRANT ID. Measured on production: **three unrevoked, never-expiring grants lost
all 1,103 of their frames**, with six more chains already past 80 days. The design was rejected in
review (#407: "Do not ship a chain-scoped delete") and shipped anyway.
→ Audit tables are append-only and time-partitioned. A retention unit is NEVER a key that is also
a live object's identity. And note the contradiction to resolve first: `frame_count` sits inside
the SIGNED chain anchor, so any legitimate prune makes verification report tampering.

**A2. Authorisation needs object-level ownership, not namespace-level only.**
#913: `check_permission(namespace, peer_id, need, conn)` **took no key**. Any peer with write on
a namespace can overwrite any other peer's row, including claim rows used as locks — two holders,
no error. (Divergence 040, slice 2: every call now says what it touches, `resource=Key(k)`,
`Prefix(p)` or `NAMESPACE_WIDE`, and the decision still reads the namespace alone until the
capability decision, slice 4. Slice 3: that decision is one database function,
`meshy_namespace_permission` (`sql_meshychat/015`), which `check_permission`, `memory.list`'s
enumeration and `/v1/mesh/wait`'s visibility call; the dashboard's owner-session queries still
read the grant table themselves, by the design's decision. Slice 4: a presented capability
decides the key, `check_permission(..., principal=)` asking `apps/memoryskills/capability.py`,
and a refusal never falls through to the grants; it is dark until slice 6 carries a token to
a handler, so every call today still decides by the namespace alone.)
→ One authorisation predicate, implemented as a database function, called by every layer and
re-expressed by none. Row ownership enforced at the storage boundary, not in views.

**A3. Signatures must stay attached to the exact bytes they signed.**
#596: `skill.result` signatures were persisted detached from their payload, and egress re-attached
them to a projection — so no delivered envelope could verify. Retrofitting invalidates every
stored row, which is why this is a before-release decision.
→ Persist the canonical signed bytes; sign at egress or store the envelope whole.

**A4. Gates default CLOSED, and a skipped check is a failure.**
#904: seven of seven dependency-audit runs reported success while the audit step read `skipped`,
missing a RUSTSEC advisory on the production surface. #462: the CI attest route signs a
**caller-asserted** pass boolean. About 18 `*_ENFORCE` flags defaulted off.
→ No enforcement behind a default-off flag. A killed, skipped or errored check must never read as
a pass.

**A5. Per-seat identity, at the forge and on the mesh.**
#810: every PR author is one shared credential, so a non-author review is refused as self-review
and `reviewDecision` is permanently empty — the review policy is unenforceable by construction.
#673: 78% of mesh traffic carried `UNVERIFIED-FROM`; a marker that fires on almost everything
detects nothing.

---

## B. Rules that make a whole defect class unrepresentable

**B1. Every connection is RLS-bound by construction.**
There is one pool constructor, it takes a principal, and it does `SET ROLE` + the local setting.
There is no way to obtain an unbound connection. *The SET ROLE hook must be provable per
connection; `pg_stat_activity` cannot answer it.*
**TRUE here as of 2026-09-23, and enforced.** Every connection under `apps/` comes from ONE
constructor, `apps.core.dbconn.connect()`, which does `SET ROLE meshychat_app` (it was `meshy_app` on 2026-09-23) and then OBSERVES
that it took (`assert_not_bypassing`), resolves the ONE configured database (`dbconn.dsn()`),
and hands back an autocommit connection so `SET LOCAL` binds to a real transaction. The only
other opener is `apps/core/sqlmigrate.py` (the migrator CLI, which must run as the owner
because migration 010 creates the app role), named as an exemption with its reason.

How it got there, because the correction history is the lesson. On 2026-09-21 this rule was
marked "already true" and was not: six call sites opened a connection (dbconn, the migrator,
and four app modules each opening their own: `apps/authapi/db.py`, `apps/peers/db.py`,
`apps/memoryskills/db.py`, `apps/wsgw/db.py`). All four bound the role, so the PROPERTY held
while the MECHANISM did not. Three moved onto `dbconn.connect()` afterwards.
`apps/memoryskills/db.py` was last, and it showed why the mechanism matters more than the
role: its role binding was right, so every role check passed it, but it resolved its
DATABASE from `MESHY_TEST_DSN` (a test variable) with a hard-coded dev fallback, ignoring
MESHY_DSN, DATABASE_URL and settings. Anything that took a connection from it would have run
the memory skills against the dev database while every route used the configured one. It now
delegates to `dbconn` and also crosses `assert_serving_connection`, the probe that proves RLS
is in force on a FORCE-RLS table. The request path's in-process memory dispatcher
(`apps/calls/views.py` -> `apps/calls/route.py`) never used it: it runs the handlers on the
request's own `dbconn.serving_connection`.

`tests/test_every_connection_is_role_bound.py` holds the rule by AST, not text:
`test_no_app_module_opens_its_own_connection` refuses any opener under `apps/` other than
`dbconn` and the named exemption, so "one constructor" cannot drift back into a convention.
`tests/test_connection_role_is_verified.py` checks the same at RUNTIME, per helper, by asking
the database what role and which database each one actually reached.

**What the one constructor does NOT give by itself:** the RLS CONTEXT. `set_role` makes
policies apply; which rows they admit still depends on the transaction-local settings each
caller enters (`rls.peer_context`, `rls.server_internal`, and for the tables derived from
shared_memory, `rls.projection_writer`, which only background jobs enter: divergence 022).

**B2. Tests run under the production role.** A superuser test pool makes RLS invisible and turns a
green suite into a false negative. A superuser test connection should be a build error.

**B3. Absence is typed.** Repositories return `NotFound | Denied | Empty` — never a bare `[]`.
*(An RLS-scoped select with no context returns zero rows; a permission-scoped read returns `[]`; a
wrapper defaulting a missing envelope path to `[]` reported "the mesh is empty" across 818 heads.)*

**B4. Every cross-principal reference is a composite foreign key.**
`granter_peer(id, owner_id)` rather than `granter_peer_id`, so a forged grant is unrepresentable
rather than merely detected. *(IG4: a grant naming a peer owned by someone else was accepted.)*

**B5. Every query has a declared cost bound, enforced by a test.**
`reconcile_chain_head` COUNTs an entire chain on every grant-bound authorize — O(n²) per chain,
**~490 GB/day of production disk reads**, against `shared_buffers = 128 MB` on a 30 GB database.
The hourly `run_frame_audit` re-hashes the whole table on top (~44 GB/day).
→ No per-request full-table or full-chain aggregate. A plan that exceeds its declared bound fails
the test. **This is the rule Django's ORM most easily violates**: `.count()` on a related manager
reads like a cheap call.

**PARTIALLY ENFORCED as of 2026-09-21**, by `tests/test_query_cost_bounds.py`. Before that
nothing enforced it at all: no test anywhere asserted a plan, a cost, or a row estimate.

It asserts SCALING rather than a cost number, because a cost bound checked against a small dev
database passes vacuously (Postgres correctly prefers a sequential scan on a tiny table, so
every plan looks cheap) and absolute costs do not travel between machines or planner versions.
The head lookup is measured at two chain lengths and must not grow; a query known to scale is
measured alongside it and must be seen to grow, because a bound whose instrument cannot detect
growth proves nothing.

**One measurement it produced, recorded rather than filed as a defect.**
`apps/memoryskills/handlers.py:981` runs a `COUNT(*) ... GROUP BY key` on every
windowed-history `memory.query`. Its key array is bounded by `limit`, so the number of chains
is bounded; the count inside each chain is not, and `created_at` carries no index on
`shared_memory`, so that predicate filters rather than bounds. That is this rule's named shape
at a much smaller scale than the 490 GB/day incident, and the bounded key count is what makes
it a different order of problem.

**Stated limit.** The bound runs against a session-local temporary table carrying the live
index definitions, because the dev database is shared with other sessions and planting rows
plus `ANALYZE` would perturb their plans mid-run. So it proves the query SHAPE scales as
claimed. It does not prove anything about production statistics and cannot catch a plan
regression caused by real data skew.

**2026-09-26 (E16): the recorded measurement is bounded, and every aggregate is on a ledger.**
The windowed-history count now reads each key's lowest `HISTORY_WINDOW_COUNT_CAP + 1` (101)
versions through the `(namespace, key, version)` index, so it reads at most keys x 101 rows
whatever the chain length: measured at 1,010 rows for ten keys at both 200 and 800 versions a
key, where the `COUNT(*) ... GROUP BY key` it replaced read 2,000 and 8,000. Past the cap the
answer is a lower bound and says so (`versions_before_since_is_lower_bound`,
`history_window.count_cap`), which the exact count does not. The test measures the
statement the handler RUNS, captured from a real `memory.query`, by the rows it reads under
`EXPLAIN ANALYZE` on the temporary table (an estimate cannot see a LIMIT inside a lateral
subquery). `AGGREGATE_LEDGER` in the same file counts the SQL aggregate literals under `apps/`
per file, so a new one fails until it is declared. It is a tripwire, not a proof: it counts per
file, so swapping one aggregate for another in the same file passes; it sees only an aggregate
written whole inside one string literal (not one built by concatenation or an f-string
placeholder, and not an ORM `.count()`); and it does not scan `sql_meshychat/`. **Still
PARTIAL:** the ledger records 43
literals in 20 files, and only this one is measured; the dashboard queries, mesh graph coverage,
meshsem related reads, meshsum facts and MeshGuard's pending count are on request paths and each
needs its own scaling test.

**B6. Pagination is server-issued opaque snapshot cursors; completeness is distinct keys.**
*(A cursor walk lapped — 21,630 rows on one pass, 39,630 with every row duplicated on the next,
both terminating "cleanly". A date-shaped key poisoned an epoch-ms cursor. A BOM reset a cursor to
zero and replayed an inbox.)*

**B7. Delivery state is keyed on (subject, claimant) and written before the irreversible action.**
*(An ack key one dimension short made a correct acker read as not-acked.)*

**B8. Bounded caches and declared ceilings.** LRU + TTL by default; no unbounded dedup set.
*(The memory daemon OOMed on an unbounded `seen` cache holding full envelopes for the process
lifetime, while `/v1/health` stayed 200 throughout.)*

**B9. Health exercises the work path and reports a content hash of the running code.**
*(A bind-mounted daemon ran stale code that no hash could identify; health was 200 while memory
skills timed out.)*

**B10. Migrations are byte-immutable and the deploy refuses on version mismatch.**
*(CRLF checksums crash-looped the mesh; a stale embedded migration set crash-looped it again 14
times in 51 minutes; the version marker is hand-typed and only checked for the PHRASE, never the
number.)*
**Already true here** — `.gitattributes` pins `sql/*.sql` to LF, and `sqlmigrate.py` refuses a
file containing CRLF and refuses checksum drift on an applied migration.

---

## C. Test discipline (these lessons cost the most to learn)

**C1. Every negative assertion has a positive control in the same test.** Already the house rule;
it is enforced in review here.
**C2. No gate merges without a red-proof in the same commit.** A gate that has never refused is
unarmed, not passing.
**C3. Prove a test can FAIL** by breaking the fixture, not by reading the code.
**C4. Match structured fields, never payload substrings.** *(A probe grepping stdout for
`50[0-9]|E_INTERNAL` matched message bodies and produced a 34% "corruption" finding that was
entirely the instrument.)*
**C5. Re-verify with a DIFFERENT instrument.** Re-reading your own output catches nothing.
**C6. A test must assert that it RAN.** *(Serial markers that were no-ops across processes;
keyless seats silently skipping the suite that was red.)*

---

## D. Operational, before anyone depends on this

- **Backups in version control, offsite, restore rehearsed** against production-shaped data.
  *(#406: the backup script was never in git; the restore check was calibrated to a schema five
  times smaller; a 2.05 GB dump of the live PII database was left inside `.git`.)*
- **Metrics with a scrape config, alert rules and a delivery path to a human.** *(#408: nine
  Prometheus series exist, with no scrape config, no alert rule and no send path anywhere in the
  tree. Production is unwatched.)*
- **Deploy must be able to roll the database back**, or must refuse to roll forward. *(Today a
  rollback restores the previous image and leaves the schema migrated; there are no
  down-migrations.)*
- **Resource ceilings pinned, guards that ABORT at the floor.** *(`nofile` 524288 after the
  2026-06-29 outage; a disk guard once printed "15G free, soft 15G floor" and proceeded anyway.)*

---

## E. From the pull-request post-mortem (688 PRs: 562 merged, 125 closed unmerged)

**The shape of the history is itself the finding: fix PRs outnumber feature PRs 2.3 to 1**, and
18% of PR titles name an earlier PR they repair. 27 PRs are rollups of 3-20 branches, up to
44,317 lines.

**E1. Fail-open is the single largest defect class (~30 PRs).** A failure and a pass looked
identical: a logout that discarded the delete result; an attest step that wrote green rows over
**107 tests that never ran**; a health monitor that exits 0 when it cannot read.
→ Every helper returns a value or raises. No bare `except` returning `[]`/`{}`/`""`. The suite
fails on that pattern (here: the local gate, `tools/ci/local_gate.py`; hosted CI does not gate, by
operator decision 2026-09-25). Every check ships with a mutation control proving it can go red.

**E2. Signature and status must be ONE value, not two.** #922: 197 production rows carry a valid
signature over `status: failed` while their own status column says `completed`. #476: a canonical
string folded any non-UTF-8 body to empty, so **one captured signature validated any binary body**.
→ Store the canonical signed payload verbatim and DERIVE status from it. A status column that can
contradict the signature it accompanies must not exist.

**E3. Meter at one chokepoint every transport passes through.** #206: the WebSocket call path was
entirely unmetered while HTTP reserved quota - found while diagnosing a real 1,000,000-call poller.
→ One service function does reserve/refund; the view, the consumer and the background task all
call it. No direct call-start anywhere else.

**E4. Name a quota window after its real period, in a type.** #593/#765: `calls_per_day` was
enforced monthly. A 30-second poller burned a 1,000/month allowance in ~8 hours and then failed
while operators waited for a midnight reset that does not exist. The rename needed a four-step
data migration, and the name had already cost an outage.

**E5. One migration numbering authority, checked before merge.** #789: two migrations both
numbered 108 merged cleanly, three seats tested clean, and the server refused to boot.
→ Django's dependency graph plus a test that rejects two leaf nodes in one app, run by the local
gate before every push (hosted CI does not gate here: operator decisions of 2026-09-25 and 2026-09-29). **Never
compile-embed the migration set** (#915/#919: a stale binary boots clean, passes health, then 500s
on a missing column).

**E6. The deploy gate asserts schema ⊇ code, not "the process started."**
→ Compare `showmigrations` to the code's expected leaf and fail hard. "It booted" is not evidence.

**E7. Background work never shares the request pool.** #385: an hourly full-table audit ran on the
request-serving pool with no yield and no timeout, producing a **2.5-minute fleet-wide stall every
hour, at a fixed minute** (the timer was anchored to process start).
→ Separate pool, per-tick timeout, jittered schedule, and the retention policy written in the same
change as the table.

**E8. Never commit generated artifacts, and never gate on them.** A corpus regeneration moved one
retrieval case from rank 1 to rank 14 and **froze main for 15 hours**; 16 further PRs are just
regenerated-graph noise.
→ Graphs, embeddings and indexes live in object storage, rebuilt by a job.

**E9. One branch, one intent.** #721 reverted a rollup that "resolved thirty conflicts by taking
one side per file", destroying work in three places and blocking every seat. If a change cannot be
reviewed alone, it cannot be reverted alone - and the revert is what you will need.

**E10. Reviewers find logic; production finds lifecycle.** 800 review comments caught nulling a
NOT NULL column, a double-spent proof, an unindexed lookup. What escaped to production was
different in kind every time: signature/status coupling, resource exhaustion, schema/binary skew.
→ Spend the review budget on lifecycle: what happens on restart, on rollback, at the quota
boundary, when the table is 100x bigger, when two versions run at once.
