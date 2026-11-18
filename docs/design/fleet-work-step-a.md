# Fleet work facts, Step A

`fleet --work` opts into `GET /v1/mesh/recipients?to=...&detail=work`, following Kxxx's signed
`8151f1` design and `3d69c5` review. Default calls and the existing fleet pane do no additional work.

For each resolved peer, the response adds `last_sent_at`, `indexed_turn_at`, `work_as_of`, `owed`, and `awaiting`.
Last sent uses the authenticated writer and the head version's server creation timestamp; editing an eligible
message can update that time. It is bounded to message keys beginning at seven days before the snapshot and server
timestamps within those seven days. An edit of an older key is outside this window. `work.sent_days` names the
window; null with a successful snapshot displays `none/7d`, while missing/failed facts display `?`. The writer has
no index: work grows with fleet traffic in the seven-day key range, not only the peer's traffic. Query-cost tests
use the actual SQL and a growing-history sensitivity control.

Indexed turn is the newest item timestamp in head-backed context chunks for the peer's resolved labels and
configured context namespace. One backward `(namespace,seat,time_last)` index range per alias stops at the first
live-head-backed chunk; Python takes the newest alias result. The head join is retained because a current indexed
generation alone does not establish a live manifest head. Head-rejected candidates remain an unmeasured cost.
The timestamp can lag and can be absent. Neither timestamp establishes a currently open or closed turn.
The client labels its indexed age `turn<=`: the indexed record gives a lagging upper bound on the age of the
latest turn, rather than measuring a live turn state. One label may resolve to several peers; each keeps its
authenticated-writer last-send timestamp, including a null timestamp for a peer with no sends in the window.

Counts use the graph's owed and awaiting predicates over fourteen days before `work_as_of`, without an output
limit. They separate `open` and `active`. A single awaiting read supplies both counts and up to two distinct
recipient labels, sampled in that read's order; this is neither a full recipient list nor proof of blocking.
The route names `work.ask_days: 14` and `work.counts_scope: recipients`.

Oxxx confirmed the broader counts-and-labels audience in signed `msg/<key>`, relayed by
Kxxx's `67804f` and `3d69c5`. Every resolved peer receives these facts, to the existing recipients/presence audience.
Another seat's owed message list and text remain excluded. The existing mesh-chat permission check still applies.
Indexed metadata is one timestamp per seat to the presence audience, consistent with the operator opening
transcripts to the seats (Kxxx's source-review confirmation); no transcript text or path is added.
Older servers and failed snapshots yield unavailable counts, never an invented zero.

Facts are held for 60 seconds per process in a cache capped at 4,096 entries. The key includes database, peer,
resolved labels and context namespace. Failed reads are not cached. A savepoint isolates each optional work read,
so an SQL failure leaves that peer's ordinary presence/mail fields and other peers' work facts available.
A warning names the peer and exception type without copying exception text. The cache is declared as capped LRU
with a sixty-second hold in `tests/test_bounded_caches.py`'s ledger.
`--work` requests four labels per chunk, rather than plain fleet's ten, to reduce exposure to the client's 60-second
HTTP timeout. Real route-role timing across the fleet remains required before deployment; chunk size alone does
not prove a timeout bound. No timer, polling loop, reporter, migration or additional pane load is installed.

Step B's separate turn-reporting route, schema, transcript tails, working/quiet/idle inference and idle-over alert
are not part of this draft. Step A alone does not meet A+B acceptance. They wait for the named predecessor PRs
and their own implementation and validation.
No client installation, server deployment, production configuration change or HZ update is performed here.

Validation is delegated to HQB: the new server/cache tests, existing recipients regressions, new client tests and
existing fleet regressions, `tests/test_query_cost_bounds.py` and `tests/test_bounded_caches.py`, sequentially.
The indexed-time regression uses actual head/non-head chunks with interfering seats and namespaces; the targeted
mutation list also removes the head filter. Local static checks are separate from execution evidence.
