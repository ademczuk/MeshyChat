# Phase B runner: review and installation proposal

Status: source candidate only. No scheduler/task, installed checkout, identity,
bank, route, service, launcher or phase C adapter is changed by this proposal.
Pxx reviews source; memory-bxxxxx runs the HQB gate. Kxxx installs only after
those results and explicit operator authority. Review is not live qualification.

## Invocation and private state

From an owner-pinned, reviewed checkout, using the existing Python environment:

```powershell
python -m tools.postbox.handover watch-once --seat codex-kxxx-cli --policy C:/Users/user/.meshychat/handover-phase-b/codex-kxxx-cli/policy.json --root C:/Users/user/.meshychat/handover-phase-b/codex-kxxx-cli
```

Proposed policy (replace the forty zeroes with the reviewed source revision;
the field records owner-declared provenance, not a verified Git attestation):

```json
{
  "schema": 1,
  "seat": "codex-kxxx-cli",
  "source_commit": "0000000000000000000000000000000000000000",
  "runtime_epoch": "owner-unmeasured-runtime-1",
  "dispatch_enabled": false,
  "limit": 245000,
  "scope": "total",
  "warn": 183750,
  "request": 196000
}
```

`dispatch_enabled: false` records observation and prepared notices privately.
Changing it to true permits the existing gated sender; that change does not
allocate a new generation. It requires owner installation approval, not an
inference from a successful test or a source merge. There is no bypass sender.

The installed identity is exactly `codex-kxxx-prod`. The config identity's UUID
must match `codex:<UUID>` in its bank's consumer field. A second identity using
the UUID is ambiguous. A bounded traversal of the entire YYYY/MM/DD sessions
tree must find exactly one matching rollout, with a complete, bounded first
`session_meta` record naming the same UUID. Exhausting the traversal budget
refuses uniqueness even if one candidate has already been found. Modification
time never chooses a rollout. Route and policy are reread before allocating a
generation and before each dispatch. These are sequential file reads, not a
transaction with postbox; see remaining qualification below.

The private root contains:

| Path | Purpose |
| --- | --- |
| `runner.lock` | Exclusive owner; never stolen based on age or PID |
| `binding.json` | Route/policy fingerprint and monotonic generation |
| `g000001/metrics.json` | Existing bounded observer state and normal outbox |
| `g000001/faults.json` | Independent fault outbox |
| `notice-journal.json` | Frozen bodies, keys, operation IDs, intent and delivery evidence |
| `notices/hb1.<digest>.txt` | Derivative body file for the gated sender |
| `local-status.json` | Sanitized result or incomplete-evidence HOLD |

An existing locked owner is never replaced automatically. A crashed lock
requires the owner to prove the old process is gone before manual recovery.
Malformed evidence/journals are preserved. Windows ACLs and power-loss
durability remain installation qualification items; POSIX mode bits alone do
not prove Windows privacy. Private metrics can contain partial transcript bytes.

## Notice identity and recovery

The normal operation ID is `hb1.` plus SHA256 of the canonical JSON array
`[seat, session, generation, kind]`. Fault IDs additionally include the observer
epoch and canonical reason/degraded set from the independent fault outbox.
Normal WARN, REQUEST and HOLD are distinct operations. Bodies contain only an
explicit allowlist: binding, action/reason, configured thresholds, numeric
observation evidence, owner-declared revision and qualification status. Raw
transcripts, partial records, credentials and installed configuration never
enter notice bodies. The first observation freezes the evidence; later samples
cannot rewrite the body associated with an operation.

All prepared notices are journaled before any dispatch. An intent is committed
before calling `codex_kxxx_cli_send.py` with the frozen `--op-id`, exact body,
TO `kxxx`, no CC, and thread `<thread>`. The wrapper's signed
client completion, operation ID, body, sender label, routing and canonical
storage receipt must agree before recording delivery. A successful delivery
is not a reply, handling acknowledgement or durable receiver ACK/cursor.
These notices do not request an acknowledgement.

A crash before/after sending or before the local receipt commit reuses the same
operation ID and body. It relies on the existing client's operation journal;
this is not a claim of network-level exactly-once delivery. Retry is refused
after six days or a backwards clock, leaving grace inside the client's stated
seven-day retention. No pruning or fresh ID hides an uncertain send. An
unresolved operation from an old generation requires owner reconciliation;
it blocks further dispatch rather than being silently discarded or rerouted.

The fence compares the stored generation as well as the route fingerprint:
an A-to-B-to-A route round trip cannot resurrect an earlier operation. A long
combined degraded reason retains its complete canonical set in the private
notice key, while the outbound reason uses a bounded `degraded-set:` digest.
Accumulating legitimate degraded reasons must not starve the fault outbox.

One invocation scans at most 64 chunks of 262144 bytes (16 MiB), with the
observer's two-second soft budget between chunks. Discovery has a 20000-entry,
two-second soft budget and bounded metadata. Exactly one sender invocation is
allowed per run. It waits at most 130 seconds, then attempts a bounded Windows
process-tree reap. No task-completion event is interpreted as process exit.
No raw subprocess output or exceptions are copied to public status.

## Accounting and phase C

The configured total limit is 245000, WARN 183750 and REQUEST 196000. Accounting
and runtime epoch qualification are **UNQUALIFIED**. The CLI accepts no policy
field that can turn a numeric limit into qualified evidence; ratio triggers
stay disabled. The owner must separately measure binary/version, model/profile,
configuration, scope and exact epoch before a future qualification change.

The existing same-task compaction-rate and rollout-size maintenance triggers
remain independent of ratio qualification. Effects stay OFF and phase C stays
HOLD: no checkpoint cutover, launch, restart, retirement, route write or
configuration write is implemented by this command.

## Scheduler proposal only

Propose one hidden Scheduled Task owned by Kxxx: every 60 seconds plus logon,
using `pythonw.exe` in the reviewed checkout, private root above, a three-minute
execution bound and `IgnoreNew` multiple-instance policy. Child processes must
stay hidden. The exclusive runner lock remains the second owner fence.
No task XML installer, registration command or periodic polling loop is shipped.
Kxxx should review task account, ACLs, working directory, executable and checkout
pins before installation. An overlapping invocation must not steal a stale lock.

## Validation and remaining qualification

Pure fixture file: `python -m unittest tools.postbox.handover.test_watch -v`.
Mutation controls: `python -m tools.postbox.handover.check_watch_mutants`.
The repository gate collects `tests/` only, so `tests/test_handover_gate.py`
adapts both handover unittest modules and the mutation driver into three gate
tests. It runs each module serially, refuses zero collection or nested skips,
and exposes nested failures/errors. The mutation adapter omits the HQD health
flag; that flag only adds production probes and never changes test semantics.
The wrapper can be checked without pytest or a database with
`python -m unittest discover -s tests -p test_handover_gate.py -v`.
The mutation harness creates isolated temporary package copies and requires
assertion failures; import errors, runtime errors and timeouts are failures of
the mutation check. It runs only one test file/case at a time. On HQD run from
a `.cmd` launcher with `start /low /b /wait`; use `--check-hqd-livez` for a
bounded health check around each mutant and stop if production livez falters.
Run the existing `test_handover` file separately, never concurrently. No full
suite, database test or build belongs on HQD.

Still required before installation: Pxx source review, HQB gate, owner source
and sender pins, Windows ACL/power-loss durability, task/process-tree lifetime
qualification, actual route-race qualification, and a fresh bounded live notice
whose signed storage and receiver handling/receipt contract are independently
verified. No such live challenge was run by these fixture tests. Sequential
route rechecks cannot exclude a retarget in the instant after the last read.
Source revision and runtime epoch strings are owner declarations. They do not
prove the running binary/model, and do not enable accounting or phase C.
