# Transcript context: server contract gaps

Read-only source audit against main
`6c67f65f7a415ca5cbd6e73b72fc2963f90dbd65`. These findings describe that code,
not the live deployment, enabled skill rows or current grants. Kxxx owns the
server/schema implementation; memory-bxxxxx reviews it before a gate.

| Existing source contract | Required context contract / proof |
| --- | --- |
| `apps/meshsem/views.py` gates graph namespaces and applies `_card_filter` for the card namespace. Card filters are not a general context schema. | Explicitly allow the new namespace and validate peer UUID/session/committed-batch filters. Reject unsupported filters rather than quietly ignoring them. |
| `apps/meshsem/graphsearch.py` looks up allowed head/chunk candidates. Existing card keep-filters are supplied by its caller. | Apply context ownership and committed-source filters before selecting/ranking candidates. Test a noisy unrelated seat occupying the candidate pool. |
| `apps/meshsem/graphtext.py` extracts text fields and chunks them; a structured manifest is not automatically searchable dialogue. Coverage is an aggregate projection. | Index approved segment text deliberately. Prove each committed fixture's query visibility separately from its exact source read; aggregate coverage does not prove fixture freshness. |
| `apps/memoryskills/handlers.py` supports expected-head-version writes and signed version reads. This is not an atomic multi-record batch API. | Verify segment versions/digests before committing an immutable completion manifest; recover ambiguous outcomes with exact signed reads. |
| `memory.query` accepts a fixed set of filters, including namespace/key-prefix/status/tags/author/history and pagination. It has no dedicated context peer/session/manifest-commit filter. | Specify and test the exact new query contract, including bounded pages and access control. Do not assume new filter names work. |
| `apps/memoryskills/redact.py` checks owner/write capability and preserves routing fields; context identity/provenance fields are not covered by its card-specific preservation rule. | Define context tombstones, segment/manifest/recovery-state invalidation and safe retained provenance. Prove stale derived references cannot restore invalidated text. |
| Redaction deletes selected derived database rows. It does not delete native files, queue snapshots, SQLite WAL or backups. | Document each copy's retention and authorized cleanup separately; include crash-left raw prefix snapshots in local-spool tests. |
| `apps/calls/bootstrap.py` lists memory.redact, while execution also depends on registered skills and active authority. | Verify availability and authorization on the intended scratch server. Custodian reports no production skill row; source code is not deployment proof. |

Authenticated review inputs, kept distinct from measured source facts:

- memory-bxxxxx reports an 8 KiB value ceiling and per-peer burst 600 at
  5 writes/second. Confirm live settings before sizing the uploader.
- kxxx's signed `msg/<key>`, version 1, reports the
  10 October operator ruling: normal checks refuse client/engagement names in
  transcript context; the card namespace alone remains exempt. Earlier reports
  (8 October card-only exemption and memory-bxxxxx's reported 30 September
  unrestricted Mesh policy) remain attributed history, superseded here. This
  resolves the design policy; server enforcement still requires implementation
  and tests. Secrets fail closed.
- memory-bxxxxx assigns the server/schema lane to kxxx, queue/hooks to this
  draft, parser/redaction review to txxxx and Kimi dialect work to Nxxxxx.

Missing acceptance inputs: reviewed context.v1 schema and canonical serialization,
namespace and authenticated principal grants, retention/invalidation policy, a
chosen disposable `MESHY_TEMPLATE_DSN`, and two independent test-reader identities.
No server schema, namespace, grants, hooks or real-history ingestion is deployed
by this draft. The five native clients still require installed capture and fresh
compaction/restart graph-recovery evidence.
