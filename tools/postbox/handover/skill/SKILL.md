---
name: codex-handover
description: Inspect Codex compaction pressure and prepare a bounded, durable checkpoint for an authorized fresh conversation handover. Use for compaction loops or an explicit handover request, not generic restart or context-limit changes. Installed transitions remain HOLD until the local adapter is qualified.
---

# Codex handover

Use the shared interface in [../observe.py](../observe.py), following
[../README.md](../README.md) and the agreed [contract](../contract-r2.md).
This source skill is not installed by this PR. Preserve current operator intent,
restrictions, unfinished work and paused goals. Local detector notices identify
machine provenance; they are not operator instructions or seat directives.

Read the seat's existing bounded recovery entrypoint first. Identify the current
thread/home and installed policy without dumping identity files or a transcript.
Use bounded incremental local scans with a private metrics path. Do not infer a
compact limit from window size or silently qualify unknown accounting. Do not add
mesh polling to drive the detector. For unsupported platform status, report HOLD.

When pressure or an explicit authorized handover calls for a checkpoint, request
an ordinary-work fence from the qualified controller at a safe boundary. A
cooperative promise is not proof of a process fence. Record active operations,
children, ambiguous sends and deadlines; do not claim them settled without proof.
If no qualified controller exists, prepare the checkpoint, expose HOLD and leave
launch, routing and retirement to the owner. Do not change machine configuration.

Build all fifteen manifest completeness sections defined by `protocol.SECTIONS`:
operator intent/permissions/questions; goal state; consumed review budgets;
signed message keys AND versions, outcomes and reply keys; complete canonical
open-work ledger references; directives/deadlines; ACK ledgers; uncertain effects;
children; leases; worktree/HEAD/dirty state; artifact hashes; tests actually run and
pending; identity/home/config/state paths only; standing instructions by path and
size. Preserve every unresolved obligation in canonical ledgers; select only the
needed discovery text for the prompt. Never copy credentials or secret state.

Write factual bootstrap text <=6000 characters, and bootstrap plus selected
discovery <=24000. These are text budgets, not token guarantees. Carry goal pause
state explicitly. If mandatory standing content already defeats the budget,
request owner-reviewed bounded entry instructions and HOLD. Do not truncate it.
Populate completeness counts/digests and text hashes, validate with
`protocol.checkpoint`, then save with `protocol.save_checkpoint`. Schema validity
does not attest current authority, backlog completeness or safe quiescence.

A qualified controller owns the journal, one fresh launch attempt, enforced
standby, service/queue cutover, authenticated incoming nonce proof and active-work
lease. Never resume/fork the old UUID as a fresh transition, retry an uncertain
effect, steal a lock, reset mail offsets or fabricate an ACK/cursor. A five-minute
ready timeout retains the last verified checkpoint and uncertainty journal in
HOLD. Respect the 30-minute post-success cooldown.

The successor reads only the validated bounded bootstrap and needed references,
then runs qualified resume-brief discovery beneath the direct task and newer
authenticated instructions. Missing dependencies require a reviewed signed
fallback or HOLD. Standby permits scoped verification only. Ordinary work starts
after the controller proves the old fence, loaded route, actual incoming nonce
consumption/reply and active lease. Preserve uncertain sends until evidence
resolves them; at-least-once mail does not guarantee exactly-once side effects.
