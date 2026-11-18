# CLI error metadata

CLI failures add `code` and `retryable` fields to their existing JSON details.
Successful output and exit statuses keep their existing contracts. The CLI does
not automatically retry operations.

| Failure | Code | Output | Exit |
| --- | --- | --- | --- |
| HTTP response | `E_HTTP` | stdout; retains `http_status` and `body` | 1 |
| Client refusal | `E_CLIENT_REFUSED` | stderr; retains `refused` | 2 |
| Transport failure | `E_UNREACHABLE` | stderr; retains `unreachable` | 2 |
| Local file or other local I/O failure | `E_LOCAL_IO` | stderr; retains `unreachable` | 2 |

Read failures retain their typed `error` and all existing details; `code` repeats
that error. A failed direct read exits 1. `listen --read` and `inbox --bodies`
can return failed inline reads while exiting successfully: inspect each
`read_error`, including `E_HIT_HAS_NO_VERSION`.

For read commands, `retryable: true` identifies transient HTTP statuses 408, 425, 429, 500, 502,
503 and 504; timeouts and recognized connection failures; temporary DNS
failures; and read refusals with `E_INTERNAL`, `E_UNAVAILABLE`, `E_TIMEOUT` or
`E_RATE_LIMITED`. A pending read whose polling budget expires is
`E_READ_POLL_TIMEOUT`. It does not establish that the server refused the call.

Permission failures, unknown keys, invalid or unverified responses, certificate
failures, and unclassified errors are not marked retryable. WebSocket listener
close codes retain their numeric meaning and are separate from these fields.

Mutating commands report `retryable: false` and `outcome: "unknown"` for timeouts,
connection resets, and ambiguous HTTP failures (including 500/502/504). Reconcile
the existing operation before repeating it. Writes are marked retryable only for
a refused connection before acceptance, or HTTP 429/503 without a call ID before
any call was accepted. After acceptance, even a refused poll connection or a busy
poll response leaves the write outcome unknown. New commands default to this
conservative write contract. Read-only callers can apply a bounded retry policy.
Completed attachment uploads also count as partial progress: a later send failure
must not advertise a safe repeat that would upload fresh copies of retained files.
Windows WSA network errors receive the same transport classification as their
POSIX equivalents.

For a send with `outcome: "unknown"`, reconcile its existing operation with
`send status <op-id>` using the operation ID from `send --op-id`. Check that
operation's status before sending again. Both `send status` and `send drain`
can resubmit unresolved operations and therefore use the write error contract.

Successful authenticated reads retain attachment metadata from the verified
message value. Error metadata does not relax message or status verification.
As before, a server-signed response whose author signature cannot be verified
is printed with `signature_verified: false` and `status_verified: false` and
exits 0. This also applies when attachment metadata differs from the author's
signed value. Callers must inspect those verification fields before trusting it.
