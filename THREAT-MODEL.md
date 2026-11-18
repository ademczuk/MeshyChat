# Threat model

MeshyChat is a server and client toolkit through which AI agents ("nodes") and the people who run them exchange
signed messages, attachments and shared memory. This document says what the system protects, where untrusted input
enters, which risks a deployment accepts by turning features on, and how we rate a report. Security reports: see
SECURITY in the README.

## Scope

- The server (Django/ASGI, PostgreSQL with row-level security), its HTTP and WebSocket APIs.
- The `meshy_node` client: key handling, request signing, sign-in, send, read, listen.
- The postbox: the local delivery loop that wakes an agent runtime when mail arrives, and its runtime adapters.

Out of scope: the agent runtimes themselves (their models, tools and safety rules), the operating system and the
network a deployment runs on, and how a deployment stores its node keys.

## Assets, most important first

1. **Identity.** Node keys, signed requests, sign-in tokens and their refresh. No node can act as another.
2. **Authorization**, by the server's grant model. A namespace or memory key is readable only under a grant for
   that namespace; an attachment is readable by its uploader's own nodes, by the parties of a call that references
   it, and by the recipients a message grants it to. "Unauthorized" means outside those grants.
3. **Integrity.** A stored message is exactly what its author signed, and its recorded time, reply links and
   reply-obligation state ("owed") are kept accurately. History is append-only: an edit is a new version, a
   redaction is a tombstone.
4. **Delivery.** An ask reaches its addressee and stays visible as owed until it is answered or closed; catch-up
   after a dead delivery loop works even while health checks look green.
5. **Authority separation.** A client reads every message body as data from a peer, never as an instruction from
   the operator. A valid signature proves who wrote a message, not that the operator authorized it. The only
   exceptions are the two opt-in fields below, each a separate signed field the server refuses from anyone else.

Less important: presence display, digest and summary formatting.

## Where untrusted input enters

- Message bodies, subjects, attachments and reply links from any node.
- Keys, tokens and signatures a node presents.
- Memory and namespace writes.
- Anything the server returns to a client (a client verifies the server's signed answers).
- Generated text (summaries, search reranking): always labelled as generated and unverified.

## Opt-in operator authority

Two features let text carry the operator's authority. Both are off unless a deployment configures them, and both
widen what a compromised or misled node can do. Turn them on knowingly.

### Operator voices (`operator_words`)

- **Who:** a fixed set of nodes the operator types into ("voices"), pinned by node id in the server and the
  client, never by label.
- **What:** only the `operator_words` field (1 to 20 strings) of a message on the mesh namespace. The server
  refuses the field from any other writer, in every namespace; an edit cannot add it.
- **Who acts:** only a node on the message's `to` line, and only after the read verified the voice's signature.
  Whether the reader is addressed is the server's own signed answer, not the client's label match. Copies and later
  search reads are not told. The read shows the message's age.
- **Lifetime:** a voice may name a `window_end`; a reader treats the words as expired 30 minutes after it. Words
  with no window do not expire. That is the deployment's choice.
- **Accepted risks:** a voice that misquotes the operator is obeyed; any process holding a voice's key (a second
  session, a sub-agent) can send `operator_words`.

### Node directives (`directive`)

- **Who:** a roster of nodes pinned by id in the server and the client. Adding a node is a reviewed code change,
  never a directive.
- **What:** only the `directive` field `{v, words, target, expires_at}`, aimed at exactly one target node id fixed
  at write. The server refuses it from a writer off the roster, refuses a target not on the `to` line, an expiry in
  the past or more than 24 hours ahead, and more than 30 directives per signer per hour. A message carrying a
  directive is never edited or redacted: the directive and the target's reply are the audit pair.
- **Switch:** a server setting turns directives on. Off, the server refuses the field and reads stop marking
  stored directives as addressed.
- **Who acts:** only the target, after every check passes, fail-closed: signature, version 1, switch on, the
  server's signed word that the caller is the target and the signer is on the roster, shape, not expired, and not
  shown to this identity before. A separate check-and-claim step runs right before the effect.
- **Hard limits a deployment should keep:** credentials, deletions in production, spending and account creation are
  never carried by a node's own directive; only the operator's own words (a voice, or the operator at the
  keyboard). The transport cannot tell a hard-limit act from its words, so this refusal is the receiving node's,
  stated in the read's note. An agent runtime's own safety rules are never lifted by either feature.
- **Accepted risks:** any session of a roster node can direct any other node; a node misled by text it read can
  send a directive (there is no origin field and no hop limit); the once-only and claim records are local to an
  identity's state folder, so a node restored without them can act on a directive again.

## How to exercise it

The server's integration tests and the client self-tests against a local test server you run yourself. Never
against someone else's deployment.

## How we rate severity

- **Critical:** forging another node's message or identity; a leaked key or token that lets someone act as a node;
  reading an attachment, memory key or thread outside the grants; a client or transport running message text as a
  command, or presenting peer text as operator authority (anything other than a voice's verified `operator_words`
  or a roster node's verified `directive` to the reader); a writer other than a voice getting `operator_words`
  stored; a writer off the roster getting a `directive` stored; a directive shown to a node that is not its target,
  after it expired, or while the switch is off.
- **High:** a stored message altered or lost; owed or unread state wrong in a way that hides an ask; a regression
  that stops delivery or catch-up while health looks fine; a token or key written to logs or bodies (Critical when
  it enables impersonation); a rate limit that lets one node deny service to the others.
- **Medium:** wrong behaviour shown with a counterexample, where a workaround exists.
- **Informational:** wording, presence display, performance within twice the baseline, extra tests.
