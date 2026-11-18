"""What jobs exist. One declaration, and every entry point iterates it.

Same shape as apps/core/schema_sets.py and for the same reason: the set of things that
must run is a fact about the system, so it lives in code where adding one is a tuple
entry, not in a runbook where adding one is a thing somebody remembers.

EVERY FIELD HERE IS A RULE FROM docs/V1-DESIGN-RULES.md MADE INTO AN ARGUMENT:
  timeout_secs  E7, a per-tick timeout, enforced by the DATABASE via statement_timeout
                rather than by a Python wall-clock check that cannot interrupt a
                blocking driver call.
  jitter_frac   E7, so instances that booted together do not share a minute forever.
  max_batch     B5, a declared cost bound. A job with no bound is refused at import.
  lease_secs    how long a crashed holder blocks its own job. It must exceed the
                timeout, or a job can still be running when its lease lapses and a
                second worker starts a concurrent copy.
"""
from __future__ import annotations

import dataclasses
from typing import Protocol

from apps.calls import chain_head, frame_audit, frame_retention, heartbeat
from apps.calls import janitor as call_janitor
from apps.calls import nonce_ledger
from apps.calls import retention as call_retention
from apps.jobs import sweeps
from apps.mesh import projection as mesh_projection
from apps.meshsem import ctxjob as mesh_ctx
from apps.meshsem import ctxindex as mesh_ctx_index
from apps.meshsem import embedjob as mesh_embed
from apps.meshsem import graphwalk as mesh_graph
from apps.meshsem import related as mesh_related
from apps.meshsum import admission as summary_admission
from apps.memoryskills import legacy_verdict


class JobBody(Protocol):
    def __call__(self, conn, job: "Job") -> int: ...


@dataclasses.dataclass(frozen=True)
class Job:
    name: str
    interval_secs: float
    timeout_secs: float
    jitter_frac: float
    max_batch: int
    run: JobBody
    doc: str = ""
    #: Tick on a thread of its own inside `run_jobs --loop` instead of in the shared serial
    #: pass. For a body whose tick can outlast timeout_secs because part of it is not SQL
    #: (the files janitor's object deletes): statement_timeout cannot bound that part, so
    #: in the shared pass a slow store would hold every other job behind it.
    own_lane: bool = False

    def __post_init__(self) -> None:
        if self.max_batch <= 0:
            raise ValueError(
                f"{self.name}: max_batch must be > 0. A job with no declared cost bound "
                "is the shape rule B5 exists to refuse."
            )
        if self.timeout_secs <= 0 or self.interval_secs <= 0:
            raise ValueError(f"{self.name}: interval_secs and timeout_secs must be > 0")
        # There is deliberately NO check that lease_secs exceeds timeout_secs. It is
        # derived below as 3*timeout+5, so such a check could never fail for any legal
        # input, and a branch with no path to red is not a guard, it is decoration that
        # reads like one. The invariant is real and is asserted where it CAN fail, over
        # a range of timeouts, in tests/test_jobs.py.

    @property
    def lease_secs(self) -> int:
        """Long enough to outlive a timed-out run plus its rollback, and no longer.

        Derived rather than configured so it cannot drift below timeout_secs when
        somebody tunes one of them.
        """
        return int(self.timeout_secs * 3) + 5


def _sweep_envelope_nonces(conn, job: "Job") -> int:
    """The first registered job, and it was waiting for one.

    apps/calls/nonce_ledger.sweep_expired_nonces has existed with ZERO callers: measured
    2026-09-21, the graph reports in=0 for it. sql/049 says why it was written, and the
    index it needs is already there: "Expired rows are pruned by the TASK-33 #3 janitor;
    this index makes the sweep cheap and bounds table growth." The janitor is this.
    """
    return nonce_ledger.sweep_expired_nonces(conn, limit=job.max_batch)


#: Every job. Adding one is a tuple entry; nothing else needs telling.
JOBS: tuple[Job, ...] = (
    Job(
        name="envelope_nonce_sweep",
        interval_secs=300,
        timeout_secs=10,
        jitter_frac=0.2,
        max_batch=5000,
        run=_sweep_envelope_nonces,
        doc="Delete envelope_nonce_seen rows past expires_at, in bounded batches.",
    ),
    Job(
        # The catch-up half of the mesh_edges projection (the project's design log (not part of this release) 014). The
        # body returns rows READ, and the runner reports more_work_pending when that hits
        # max_batch, so a backlog drains tick after tick instead of one batch per minute.
        # 60s is the interval, not the latency: a fresh row is in the projection within
        # one tick plus jitter, which is the bound the inbox route documents.
        name="mesh_edges_catch_up",
        interval_secs=60,
        timeout_secs=20,
        jitter_frac=0.2,
        max_batch=mesh_projection.DEFAULT_BATCH,
        run=mesh_projection.run_catch_up_job,
        doc="Project new shared/mesh-chat rows into mesh_edges, in bounded batches.",
        # A lane of its own (15 Nov 2026). Every ask's owed state, every thread and every reply edge is read
        # from mesh_edges, and this job is its only writer. In the shared serial pass a slow job held it: it
        # ran every 2 to 5 minutes on production (gaps up to 309 s, 10 Nov), so a seat that had answered an
        # ask was still told it owed the reply minutes later. Its body is SQL only, bounded by timeout_secs.
        # The lane costs one more thread in the jobs process, and while a tick of it is claimed the runner
        # holds a control and a work connection for it (measured on #638: 4 open at the peak against 2).
        own_lane=True,
    ),
    Job(
        # the project's design log (not part of this release) 017 and 041 S5. Runs every 5 s. Since 041 S5 a call to an
        # offline target is `queued`, and this sweep is what ends it. `run_call_worker`
        # (apps/calls/worker.py) runs it in a 5 s
        # loop of its own, because the shared loop runs every job in one sequence and the
        # link job below can hold one tick for 45 + 20 s; the shared loop keeps it too, and
        # the lease decides who runs a tick. The sweep is an index range scan on
        # skill_calls_deadline_sweep_idx, bounded at max_batch rows.
        name="call_deadline_sweep",
        interval_secs=5,
        timeout_secs=10,
        jitter_frac=0.2,
        max_batch=1000,
        run=call_janitor.run_deadline_sweep_job,
        doc="Time out in-flight skill_calls past deadline_at, with a timeout event.",
    ),
    Job(
        # Semantic vectors of shared/mesh-chat HEAD rows (the project's design log (not part of this release) 019). The
        # same keyset walk as mesh_edges_catch_up, with two more bounds because a row
        # here can cost a GPU call: max_batch caps rows READ (B5), embed_per_tick caps
        # rows EMBEDDED, and timeout_secs is also the tick's WALL deadline for the model
        # calls, which statement_timeout cannot see. The deadline plus one call's own
        # timeout stays under lease_secs (3*30+5), so a lease never lapses mid-tick.
        # With MESHY_EMBED_FORBIDDEN_ROWS closed (the default) every row written today
        # is skipped as embed_policy 'forbidden' and no content reaches any model.
        name="mesh_embed_catch_up",
        interval_secs=60,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=mesh_embed.DEFAULT_BATCH,
        run=mesh_embed.run_catch_up_job,
        doc="Embed new shared/mesh-chat head rows into mesh_embeddings, per space.",
    ),
    Job(
        # Persisted related_to edges (the project's design log (not part of this release) 032), AFTER the embed job: it
        # never reads past the embed watermark or the mesh_edges watermark. max_batch caps
        # rows READ (B5); related.RELATE_PER_TICK caps sources RERANKED (about 0.6 s each on
        # HQB); timeout_secs is also the tick's WALL deadline, checked between sources, and
        # one source's calls share a 20 s budget, so a tick ends inside 45 + 20 s, well
        # under lease_secs (3*45+5). With no enabled calibration committed the job records
        # `not_calibrated` and calls no model at all.
        name=mesh_related.JOB_NAME,
        interval_secs=60,
        timeout_secs=45,
        jitter_frac=0.2,
        max_batch=mesh_related.DEFAULT_BATCH,
        run=mesh_related.run_job,
        doc="Score cross-thread related messages with the reranker into mesh_related_edges.",
    ),
    Job(
        # Vectors and lexical terms of the namespaces MESHY_GRAPH_NAMESPACES lists besides the
        # mail (the project's design log (not part of this release) 060, docs/design/wiki-in-graph.md). Reconciles the whole head
        # set against the derived set each tick, so it runs every five minutes, not every minute.
        # max_batch is the chunk bound (graphwalk.EMBED_PER_TICK); timeout_secs is the tick's WALL
        # deadline for the model calls, and with one call's own budget it stays under lease_secs.
        # With the setting at its default (the mail only) a tick reads and writes nothing.
        name="mesh_graph_embed",
        interval_secs=300,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=mesh_graph.EMBED_PER_TICK,
        run=mesh_graph.run_graph_job,
        doc="Embed and lex the head rows of MESHY_GRAPH_NAMESPACES (the wiki), chunked, per space.",
    ),
    Job(
        # v1.1 step 6 (sql_meshychat/047, apps/meshsem/ctxjob.py): committed transcript-context manifests, in commit
        # order, into turn-aware chunks and vectors on HQB's GPU. OFF unless MESHY_CONTEXT_INDEX is true: a tick then
        # reads and writes nothing. It yields whenever the mail embedder is more than one tick behind.
        name="ctx_embed_catch_up",
        interval_secs=60,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=mesh_ctx.MAX_MANIFESTS,
        run=mesh_ctx.run_catch_up_job,
        doc="Index committed shared/transcript-context manifests into mesh_ctx_chunks and mesh_ctx_vectors.",
    ),
    Job(
        # v1.1 step 6b retention (D4): transcript-context chunk occurrences older than 180 days go, in bounded batches;
        # ctx_embed_catch_up's orphan sweep then removes the vectors nothing names. Items are kept (read by key).
        name="ctx_vector_retention",
        interval_secs=3600,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=mesh_ctx_index.RETENTION_BATCH,
        run=mesh_ctx_index.run_retention_job,
        doc="Delete transcript-context chunks older than 180 days (their vectors follow via the orphan sweep).",
    ),
    Job(
        # v1.1 step 6b (sql_meshychat/053): the stored shard plan compared with the chunks, group by group, and any
        # group that differs recomputed. It reads every current chunk, so it has its own lane and a timeout its
        # lease (3 x 600 + 5 s) outlives (pxx P2-3), and it runs hourly.
        name="ctx_shard_plan_check",
        interval_secs=3600,
        timeout_secs=mesh_ctx_index.PLAN_CHECK_TIMEOUT_S,
        jitter_frac=0.2,
        max_batch=1,
        run=mesh_ctx_index.run_shard_plan_check_job,
        own_lane=True,
        doc="Compare the stored context shard plan with the chunks and recompute any group that differs.",
    ),
    Job(
        # The summary request ledger's retention, stated with its table (divergence 037,
        # sql_meshychat/017, E7): rows older than 30 days, a range on requested_at, in
        # batches of max_batch. Hourly is ample: the ledger grows by one row a request,
        # and admission bounds requests at 60 a peer an hour.
        name="mesh_summary_ledger_retention",
        interval_secs=3600,
        timeout_secs=20,
        jitter_frac=0.2,
        max_batch=summary_admission.RETENTION_BATCH,
        run=summary_admission.run_retention_job,
        doc="Delete mesh_summary_requests rows older than 30 days, in bounded batches.",
    ),
    Job(
        # the project's design log (not part of this release) 039, design section 3.3: classify shared_memory rows that carry
        # no signed_bytes (historical rows, and rows written by a legacy writer process
        # sharing the database) into shared_memory_legacy_verdict. max_batch caps rows READ and
        # classified (B5): each costs four SHA-256 candidates and one verify, well under a
        # millisecond, so a 30 s tick is mostly the anti-join. Rows are never rewritten.
        name=legacy_verdict.JOB_NAME,
        interval_secs=300,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=legacy_verdict.DEFAULT_BATCH,
        run=legacy_verdict.run_job,
        doc="Classify shared_memory rows without signed_bytes into legacy verdicts.",
    ),
    # ---- Retention (STATUS D10-D17). Every one is keyed on the row's own expiry or age
    # (rule i), deletes oldest first, and is bounded by max_batch (B5); the bodies and the
    # reason for each margin are in apps/jobs/sweeps.py.
    Job(
        # D10, the project's design log (not part of this release) 015. Nothing else prunes `dpop_jti_seen`. With DPoP on
        # by default it gains a row per peer write, so the cadence and bound are the
        # envelope sweep's; a spent jti is kept one window past expiry.
        name="dpop_jti_sweep",
        interval_secs=300,
        timeout_secs=10,
        jitter_frac=0.2,
        max_batch=5000,
        run=sweeps.run_dpop_jti_sweep,
        doc="Delete dpop_jti_seen rows more than one DPoP window past expires_at.",
    ),
    Job(
        # D11, divergence 016. A pairing code is kept a day past expiry so a late
        # presentation is still told "expired", not "unknown".
        name="pairing_code_sweep",
        interval_secs=3600,
        timeout_secs=10,
        jitter_frac=0.2,
        max_batch=1000,
        run=sweeps.run_pairing_code_sweep,
        doc="Delete pairing_codes rows more than a day past expires_at.",
    ),
    Job(
        # D11. The opportunistic prune on the challenge route runs only
        # when a challenge is issued; this is the same predicate on a schedule.
        name="auth_challenge_sweep",
        interval_secs=300,
        timeout_secs=10,
        jitter_frac=0.2,
        max_batch=5000,
        run=sweeps.run_auth_challenge_sweep,
        doc="Delete auth_challenges rows past expires_at.",
    ),
    Job(
        # D15. Runs every 300 s.
        name="owner_session_sweep",
        interval_secs=300,
        timeout_secs=10,
        jitter_frac=0.2,
        max_batch=1000,
        run=sweeps.run_owner_session_sweep,
        doc="Delete owner_sessions rows past expires_at.",
    ),
    Job(
        # D17, the flow nonce garbage collector, every 300 s. A spent nonce is its call's
        # receipt provenance, so it is kept past expiry while that call exists (operator
        # decision 2026-09-26, divergence 036).
        name="flow_nonce_sweep",
        interval_secs=300,
        timeout_secs=10,
        jitter_frac=0.2,
        max_batch=5000,
        run=sweeps.run_flow_nonce_sweep,
        doc="Delete flow_continuation_nonces past expires_at: unspent ones at expiry, "
            "spent ones once the consuming call is gone.",
    ),
    Job(
        # D13. Every 5 s, two statements
        # of at most FILE_SWEEP_CAP rows each; object deletes follow each statement, so
        # timeout_secs bounds the database part and the blob client bounds each CALL, not
        # the tick: up to 200 deletes at R2's 60 s request timeout. The janitor ticks in a
        # lane of its own here (own_lane):
        # a degraded store stalls the janitor, never the shared loop.
        name="file_janitor",
        interval_secs=5,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=sweeps.FILE_SWEEP_CAP,
        run=sweeps.run_file_janitor,
        doc="Fail pending files past the upload window; delete ready files past grace.",
        own_lane=True,
    ),
    Job(
        # D12, divergence 018: the 90-day retention sql_meshychat/005 states, which only a
        # CLI verb ran. max_batch is ONE UTC DAY a tick, not a row count: usage.prune has no
        # row bound, and a day's rows are routes x peers x outcomes.
        name="mesh_route_usage_retention",
        interval_secs=3600,
        timeout_secs=20,
        jitter_frac=0.2,
        max_batch=1,
        run=sweeps.run_mesh_route_usage_retention,
        doc="Delete mesh_route_usage days older than 90 days, one day a tick.",
    ),
    Job(
        # D14: a memory.query or
        # memory.read result an hour past its call's end becomes an audit summary; the row
        # stays. Keyed on the call's own terminal age (rule i). Its window is an hour, so a
        # minute cadence loses nothing.
        name="call_read_result_prune",
        interval_secs=60,
        timeout_secs=20,
        jitter_frac=0.2,
        max_batch=call_retention.READ_RESULT_PRUNE_BATCH,
        run=call_retention.run_read_result_prune_job,
        doc="Replace memory.query/read results an hour past the call's end with a summary.",
    ),
    Job(
        # D14: a call terminal for
        # longer than MESHYCHAT_CALL_RETENTION_DAYS (90) is deleted with its events, chunks and
        # webhook attempts (ON DELETE CASCADE). Keyed on terminal age (rule i); the tick is
        # `RETENTION_TICK_SECS` (15 minutes). Once its call is gone a spent flow nonce is reclaimed by
        # flow_nonce_sweep (D17), which keeps it only while that call exists.
        name="call_retention",
        interval_secs=call_retention.RETENTION_TICK_SECS,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=call_retention.CALL_PRUNE_BATCH,
        run=call_retention.run_call_retention_job,
        doc="Delete skill_calls terminal for longer than MESHYCHAT_CALL_RETENTION_DAYS (90).",
    ),
    Job(
        # D09, INCREMENTAL (rule ii) instead of a full-table re-hash per pass:
        # frames past a cursor on arrival, plus a bounded rolling recheck of the history
        # (apps/calls/frame_audit.py). max_batch bounds each pass. Each tick is two
        # index-ordered reads of at most max_batch frames and two chain-index probes for
        # each chained frame.
        name="frame_audit",
        interval_secs=300,
        timeout_secs=60,
        jitter_frac=0.2,
        max_batch=frame_audit.NEW_PER_TICK,
        run=frame_audit.run_job,
        doc="Audit invocation frames on arrival and re-verify the history, bounded.",
    ),
    Job(
        # E10. A grant-bound authorization must not COUNT a whole chain to find its head,
        # so each chain keeps a signed anchor. This build writes no frames, so the anchor
        # is kept by a job, and INCREMENTALLY (rule ii): each chain's signed anchor is
        # extended by the frames past its head, never recounted; a rolling pass verifies
        # every anchor (signature, head present) and extends one a late frame left behind.
        # max_batch bounds the frames the tick reads (apps/calls/chain_head.py).
        name="chain_head_reconcile",
        interval_secs=60,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=chain_head.FRAMES_PER_TICK,
        run=chain_head.run_job,
        doc="Extend and verify the signed invocation chain-head anchors, never counting.",
    ),
    Job(
        # D08 (divergence 052, operator decision D-2026-09-27-20): a chain is deleted only
        # when its root grant has been revoked or expired for longer than the owner's window
        # (invocation_frame_retention_policy, default 90 days), newest frame first, and its
        # signed anchor moves into invocation_chain_retention, where every tick re-verifies it.
        # Standalone frames only when the policy sets their window (off by default). The
        # tick is `RETENTION_TICK_SECS` (15 minutes); max_batch bounds the frames a tick deletes (B5), in calls of
        # at most CHAIN_BATCH, each a transaction of its own under the 30 s statement_timeout.
        name="frame_retention",
        interval_secs=frame_retention.RETENTION_TICK_SECS,
        timeout_secs=30,
        jitter_frac=0.2,
        max_batch=frame_retention.FRAMES_PER_TICK,
        run=frame_retention.run_job,
        doc="Delete chains whose root grant is dead past the window; keep their signed anchors.",
    ),
    Job(
        # F09: a running call's heartbeat is its target's presence (divergence 041 S2); a
        # call whose target has been absent past heartbeat.LOST_AFTER_SECS is marked LOST
        # in skill_call_heartbeats and logged; still lost heartbeat.lost_grace_secs() later,
        # it fails with E_TARGET_UNREACHABLE (divergence 069, apps/calls/heartbeat.py). 15 s, so a round
        # of the shared 30 s loop stays well inside the 120 s window.
        name="running_call_heartbeat",
        interval_secs=15,
        timeout_secs=20,
        jitter_frac=0.2,
        max_batch=heartbeat.PER_TICK,
        run=heartbeat.run_job,
        doc="Record each running call's target presence; mark calls whose target is gone.",
    ),
)


def by_name(name: str) -> Job:
    for job in JOBS:
        if job.name == name:
            return job
    raise KeyError(f"no registered job named {name!r}; registered: {[j.name for j in JOBS]}")
