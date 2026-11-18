# B07: object-level authorisation with Biscuit capability tokens (#913 / #523)

> RECORD, 2026-11-15. This is the design as revised on 2026-09-24 (base 0b6767f). It has since been built as divergence 040 (the project's design log (not part of this release), the entry on Biscuit capability tokens); read that for what the code does today. The status line and every "not built" below are as of 2026-09-24.

**Status: DESIGN, REVISED. Nothing here is built.** Branch `fin/design-biscuit`, written
against `c5457ac82a3b3113a6b3aa6f7f57478be07bf07c` (origin/main on 2026-09-23), rebased onto
`0b6767f7de1f6101f7270085f873d4cd1c3a0b71` (origin/main on 2026-09-24) and revised by an
adversarial critique against that ref. Every finding, with its severity, what it changed and
what was kept, is in the **Critique log** at the end: read it before relying on any section.
The document only. No code, no migration, no push.

**What it closes.** STATUS row B07 on `fin/status` (an internal project document (not part of this release) there, read only):
"permission checks take no key, and the delegation vocabulary (subject, scope, issuer,
executor, expiry, depth cap) does not exist", blocked on "a mechanism decision (biscuit-python
0.4.0 works ... or key-scoped grants)". The operator chose Biscuit. This document is that
mechanism, down to the Datalog, the call sites and the build order. That choice reached this
document by relay and is not yet written in the repository: on main, `README.md:82` still
lists biscuit capability tokens under "Dropped by decision", and
an internal project document (not part of this release) says the decision "must be revisited" if tokens are ever
switched on. Slice 0 revises both (Q9).

**The one finding that changes the plan before anything else.** biscuit-python 0.4.0 does
NOT install on the interpreter this server runs on here (CPython 3.14.3). No wheel exists for
3.14, the source build refuses 3.14, and the forward-compatibility build fails to compile
(section 1.1). The STATUS entry that says it "installs from PyPI with a prebuilt wheel"
measured Python 3.12.10 (an internal project document (not part of this release) at c5457ac), and that measurement does not
carry to 3.14. Slice 0 of the build order is therefore a runtime decision, not code.

---

## 0. Evidence conventions

- **This repository** is cited at `0b6767f` (origin/main, 2026-09-24) as `path:line`; the
   critique re-read every citation it changed. A citation marked `@c5457ac` is historical: it
   records the revision run by the spikes and trace.
- **STATUS B15 has landed.** It was `fin/sec-code` at `c766b76` when this was first written
  and was cited as `sec:path:line`. On main it is divergence **030** (`1da6f3b`, its review
  applied in `820ddfb`), so every such citation below is now to main.
- **MEASURED** means I ran it or read the line. **INFERRED** means reasoning from what was
  measured; each inference names the rival hypothesis the evidence does not exclude.
- **Data rule.** No mesh message text and no production row content appears here or was
  sent anywhere. The only databases touched were the test suite's own: it builds a per-run
  database from its template and drops it (section 3.4). Beyond that, the dev server's list
  of database names was read, nothing else.

---

## 1. What biscuit-python 0.4.0 actually does (the spike)

### 1.1 Install: measured on three interpreters

| interpreter | how | result |
|---|---|---|
| CPython 3.14.3 (the server `.venv` on HQD; base `pythoncore-3.14-64`) | `pip install biscuit-python==0.4.0` into a throwaway venv | **FAILS.** No 3.14 wheel, so pip builds the sdist; `pyo3-ffi v0.24.1` refuses: `error: the configured Python interpreter version (3.14) is newer than PyO3's maximum supported version (3.13)` |
| same, `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` | the escape hatch the error suggests | **FAILS.** `error[E0425]: cannot find type \`PyDateTime\` in this scope` (the binding uses a type the stable ABI does not have) |
| CPython 3.13.12 (uv-managed, throwaway, under the scratch dir) | `uv pip install biscuit-python==0.4.0` | **installs** the `cp313-win_amd64` wheel |

PyPI (`/pypi/biscuit-python/0.4.0/json`, 2026-09-23): 0.4.0 is the newest release
(0.1.0 through 0.4.0), with wheels for cp37 to cp313 (plus cp313t and PyPy) and **none for
cp314** on any platform. The sdist build log shows what the binding carries:
`Compiling biscuit-auth v6.0.0`, `ed25519-dalek v2.1.1`, `pyo3 v0.24.1`.

Nothing in this repository pins a Python version (no `python_requires`, no Dockerfile, no
`.python-version`; checked at c5457ac, and again at `0b6767f` by the critique). The only statements are measurements: 3.12.10 in
an internal project document (not part of this release), 3.14.3 on `fin/status`'s re-measured suite line.

**Re-measured by the critique, 2026-09-24.** PyPI still lists 0.4.0 as the newest release
(betas 0.4.0b1 to b3 also exist) with python tags cp37 to cp313 and pp39 to pp311, and none
for cp314. All 303 `.py` files under `apps`, `tools`, `meshychat` and `tests` at `0b6767f`
byte-compile under CPython 3.13.12 (`python3.13 -m compileall -q`, exit 0, no output). That is
syntax only: the suite has not been run on 3.13 and `requirements.txt` has not been installed
there, so Q1's pin is feasible at the syntax level and unproven beyond it.

### 1.2 Spike 1: mint, attenuate, verify, revocation ids (the 20 lines)

Run on 3.13.12. Synthetic identifiers only.

```python
from datetime import datetime, timedelta, timezone
from biscuit_auth import KeyPair, BiscuitBuilder, BlockBuilder, AuthorizerBuilder, Biscuit, Fact
root = KeyPair(); now = datetime.now(timezone.utc)
tok = BiscuitBuilder("""subject({s}); right({ns}, "", "read"); right({ns}, "", "write");
                        check if time($t), $t < {exp};""",
    {"s": "peer-A", "ns": "ns/demo", "exp": now + timedelta(hours=1)}).build(root.private_key)
att = tok.append(BlockBuilder('check if key($k), $k.starts_with("task/42/");'))   # keyless attenuation
def verify(b64, key, action, caller="peer-A"):
    t = Biscuit.from_base64(b64, root.public_key)                                  # signature chain vs root pk
    a = AuthorizerBuilder("""time({now}); caller({c}); namespace({ns}); key({k}); action({a});
        allow if subject($c), caller($c), right($ns, $p, $a), namespace($ns), key($k), $k.starts_with($p), action($a);
        deny if true;""", {"now": now, "c": caller, "ns": "ns/demo", "k": key, "a": action}).build(t)
    try: return ("ALLOW", a.authorize())
    except Exception as e: return ("DENY", type(e).__name__)
print("root  write task/42/x :", verify(tok.to_base64(), "task/42/x", "write"))
print("root  write task/7/x  :", verify(tok.to_base64(), "task/7/x", "write"))
print("att   write task/42/x :", verify(att.to_base64(), "task/42/x", "write"))
print("att   write task/7/x  :", verify(att.to_base64(), "task/7/x", "write"))
print("att   other caller    :", verify(att.to_base64(), "task/42/x", "write", caller="peer-B"))
print("revocation_ids root   :", tok.revocation_ids)
print("revocation_ids att    :", att.revocation_ids, "blocks:", att.block_count())
```

Output, verbatim:

```
root  write task/42/x : ('ALLOW', 0)
root  write task/7/x  : ('ALLOW', 0)
att   write task/42/x : ('ALLOW', 0)
att   write task/7/x  : ('DENY', 'AuthorizationError')
att   other caller    : ('DENY', 'AuthorizationError')
revocation_ids root   : ['c1d21de118c23d7b15e49686267d12b7222ae0a2a01edb271abcbebcfee880c87f102925f2a5b37b0439f603cc4e2d6ec494cba42f9d8c02df0b1547ca7e3305']
revocation_ids att    : ['c1d21de118c23d7b15e49686267d12b7222ae0a2a01edb271abcbebcfee880c87f102925f2a5b37b0439f603cc4e2d6ec494cba42f9d8c02df0b1547ca7e3305', '5c6415a5355193782755a0131b96aeee88265980851de288ba46948a4b9139eb05136efeee7a80ba7cb602531c20a28be42f5729cb591902ac176150a92fa808'] blocks: 2
```

What that measures:

- **Mint and offline verify work**, parameters passed as a dict (opaque terms, not string
  splicing), `datetime` binds to a Datalog date.
- **Keyless attenuation restricts.** The appended block has no root key; `task/42/x` still
  passes and the sibling `task/7/x` is refused, while the unattenuated token allows the
  sibling (the control that proves the refusal comes from the block, not a broken
  authorizer). This reproduces the STATUS "Ground 2" measurement on a second interpreter.
- **Revocation ids are per block and inherited.** One 64-byte id (128 hex) per block; the
  attenuated token's list STARTS with the parent's id. So revoking the authority block's id
  kills the token and everything ever derived from it, and revoking block *i*'s id kills
  that branch only.

### 1.3 Spike 2: delegation chain, over-attenuation, depth, rotation, limits, cost

The second script (`spike_chain.py`, scratch only, not committed) builds the design in
section 2 against the library: a server-minted authority block, delegation hops as
**third-party blocks signed by the delegating peer's own Ed25519 key**, a host-side holder
walk, explicit authorizer limits. The load-bearing host function, as run:

```python
MAX_DEPTH, MAX_TTL = 4, 300
def holder_walk(tok):
    a0 = limited(AuthorizerBuilder("")).build(tok)
    holder = a0.query(Rule("s($s) <- subject($s)"))[0].terms[0]
    hops, seen = 0, {holder}
    for i in range(1, tok.block_count()):
        ext = tok.block_external_key(i)
        if ext is None:                         # plain attenuation block: restrict-only, holder unchanged
            continue
        want = "ed25519/" + registered[holder].hex()
        if str(ext) != want: raise PermissionError(f"block {i} not signed by holder {holder}")
        pk = PublicKey.from_bytes(registered[holder], Algorithm.Ed25519)
        rows = a0.query(Rule("d($e, $x) <- delegate($e), delegation_expires($x) trusting {k}", {}, {"k": pk}))
        if len(rows) != 1: raise PermissionError(f"block {i}: {len(rows)} delegate facts under holder key")
        nxt, exp = rows[0].terms
        if (exp - now).total_seconds() > MAX_TTL: raise PermissionError(f"hop {i} ttl > {MAX_TTL}s")
        if nxt in seen: raise PermissionError("cycle")
        holder, hops = nxt, hops + 1; seen.add(nxt)
        if hops > MAX_DEPTH: raise PermissionError(f"depth {hops} > {MAX_DEPTH}")
    return holder, hops
```

Output, verbatim (the second run, with `max_time` widened to 100 ms; see row 20):

```
external-key repr of a peer pk: ed25519/5df8649dd0b018f7 ...
1 A uses own token, write task/42/x     : ALLOW(hops=0)
2 T presents A's token as T            : DENY(host: holder A != caller T)
3 A->E (read task/42/) E reads task/42 : ALLOW(hops=1)
4 same, E writes task/42               : DENY(datalog: authorization failed: an allow policy matched (policy index: 0), and the following checks failed: Check n°2 in)
5 same, E reads task/7                 : DENY(datalog: authorization failed: an allow policy matched (policy index: 0), and the following checks failed: Check n°1 in)
6 same, A (delegator) presents it       : DENY(host: holder E != caller A)
7 A->E->F, F reads task/42/sub/x        : ALLOW(hops=2)
8 A->E->F, F reads task/42/other        : DENY(datalog: authorization failed: an allow policy matched (policy index: 0), and the following checks failed: Check n°1 in)
9 E->F block ADDS right(write/admin)    : DENY(datalog: authorization failed: an allow policy matched (policy index: 0), and the following checks failed: Check n°2 in) | DENY(datalog: authorization failed: a deny policy matched (policy index: 1), and the following checks failed: Check n°2 in b)
10 E->F WIDER prefix task/ -> task/7    : DENY(datalog: authorization failed: an allow policy matched (policy index: 0), and the following checks failed: Check n°1 in)
11 T appends own-signed delegate(T)     : DENY(host: block 2 not signed by holder E)
12 A->E with 3600s ttl                  : DENY(host: hop 1 ttl > 300s)
13 5 hops A->..->I (cap 4)              : DENY(host: depth 5 > 4) blocks: 6
14 plain (1st-party) block delegate(T)  : DENY(host: holder E != caller T)
15 expired authority                    : DENY(datalog: authorization failed: an allow policy matched (policy index: 0), and the following checks failed: Check n°0 in)
16 kid 1 old via provider: ALLOW(hops=0) | kid 2: ALLOW(hops=0)
17 dropped kid 1: refused: BiscuitValidationError error deserializing or verifying the token
18 foreign root: refused: BiscuitValidationError
19 tampered: refused: BiscuitValidationError
20 default limits: max_facts 1000 max_iterations 100 max_time 0:00:00.001000
21 parse+sigcheck depth0 : p50=288us p99=1454us len 356
22 parse+sigcheck depth2 : p50=1137us p99=4433us len 1416
23 full decide depth0    : p50=758us p99=1763us
24 full decide depth2    : p50=2370us p99=6671us
25 authorize only depth0 : p50=326us p99=1984us
```

What each group measures:

- **Holder binding (rows 1, 2, 6).** A token is usable only by its current holder; the
  delegator itself cannot use a token it delegated away.
- **Delegation and narrowing (rows 3 to 8).** Two hops, each narrowing the key prefix; the
  action restriction (`check if action("read")`) refuses a write on hop 1.
- **Over-attenuation is refused two ways (rows 9, 10).** A delegation block that ASSERTS
  `right(... "write")` and `right(... "admin")` gains nothing: block facts are invisible to
  the authorizer's policy, which trusts only the authority block and itself. A block that
  states a WIDER prefix (`task/`) than its parent (`task/42/`) cannot widen either: every
  block's checks must hold, so the effective prefix is the intersection.
- **Forged delegation is refused (rows 11, 14).** A thief's third-party block signed by the
  thief's own key fails the holder walk; a first-party (keyless) block that states
  `delegate("T")` changes nothing because the walk reads delegate facts only under the
  holder's key.
- **Per-hop TTL and depth cap (rows 12, 13):** 300 s and 4 hops.
- **Rotation by root key id (rows 16, 17).** `BiscuitBuilder.set_root_key_id(int)` stamps a
  kid; `Biscuit.from_base64(data, provider)` accepts a function from kid to public key; a kid
  dropped from the provider is refused. Foreign-root and tampered tokens are refused (18, 19).
- **Default limits trip under load (row 20).** The defaults are 1000 facts, 100 iterations,
  **1 ms**. The FIRST run of this script used the defaults and died in the depth-2 timing
  loop with `biscuit_auth.AuthorizationError: Reached Datalog execution limits` raised from
  the holder walk's `query`. With `max_time` set to 100 ms on every authorizer the same loop
  completes. The 1 ms default is a wall-clock bound that can turn a legitimate deny into a 500
  under contention, so this design sets `max_time` to 100 ms and keeps the two deterministic
  bounds.
- **Cost (rows 21 to 25)**, section 5.1.

---

## 2. The token model

### 2.1 The vocabulary, mapped onto biscuit

| term (#913/#523, STATUS B07) | where it lives | how it is enforced |
|---|---|---|
| **issuer** | authority fact `issuer(<peer uuid>)` | the peer that asked the server to mint. Must hold `admin` on the token's ONE namespace (2.3) at MINT time and, re-read live and while `active`, at USE time (section 2.5 (b)). The server is the signer; the issuer is the accountable principal |
| **subject** | authority fact `subject(<peer uuid>)` | the first holder. At depth 0 the caller must be the subject |
| **scope** | authority facts `right(<namespace>, <key prefix>, <action>)`, one per tuple; attenuation `check if resource(...)`, `check if action(...)`, `check if operation(...)` | the authorizer's allow policy requires a right covering the request; every block's checks must also hold (intersection) |
| **executor** | delegation fact `delegate(<peer uuid>)` in a THIRD-PARTY block signed by the current holder's registered Ed25519 key | the host walk (1.3) computes the final holder; the caller must equal it |
| **expiry** | authority `check if time($t), $t < <exp>` (mandatory); each hop `delegation_expires(<x>)` plus `check if time($t), $t < <x>` | Datalog refuses after expiry; the host refuses any hop with more than 300 s of life left |
| **depth cap** | authority fact `depth_cap(<n>)`, n in 0..4 | host walk refuses more than `min(n, 4)` delegation hops |
| revocation | intrinsic: one revocation id per block | the revocation table, checked on every use (2.5) |
| root key id | `set_root_key_id(<int>)` on the authority | the verifier's kid-to-public-key provider (2.2) |

Actions in a token are `read` and `write` only. **`admin` is never mintable**: nothing that
creates or destroys authority or data in bulk (`memory.grant`, `memory.prune`, a
`pre_discipline` custody write, `namespace.claim`) accepts a token (section 4.3). `write`
does not imply `read`; the minter states both if both are meant, so a token says literally
what it allows and the authorizer carries no implication rules.

### 2.2 Root key custody

**Who mints.** Only the server, and only on request from an issuer who holds `admin` on the
namespace (the same bar `memory.grant` sets, `apps/memoryskills/handlers.py:402`). Minting a
token for another peer confers authority on them exactly as a grant does, so it must not be
cheaper than a grant. A mint is an HTTP route, not a memory skill (section 3.1), so the token
is returned once and never persisted: a skill result would be stored in
`skill_calls.result` (`apps/calls/route.py:282-288`).

**Where the private key lives.** Never in the repository and never in a committed file.
Loaded ONCE at process start by an `AppConfig.ready()` hook shaped
exactly as divergence 034 loads the fleet roster (`apps/memoryskills/apps.py:19-28`: a path
setting, read once, `ImproperlyConfigured` on anything unusable). The grants surface reads
its own flag per call instead (`apps/grants/config.py:22-24`, `lines 46-50`); that stays as it is.

- `MESHY_CAPABILITY_ROOT_KEY_PATH`: a FILE holding the hex Ed25519 private key of the ACTIVE
  minting key, with `MESHY_CAPABILITY_ROOT_KID` its integer id. **Not the
  `MESHYCHAT_BISCUIT_ROOT_SK`, which the first version reused "so an operator's secret store feeds
  both servers the same way" (critique C1).** On main, setting that variable makes
  `issue_grant` refuse EVERY skill grant (`apps/grants/config.py:46-50`,
  `apps/grants/service.py:135-140`, `lines 267-268`, pinned by `tests/test_grants.py:1330-1336`),
  so reusing it would take down grant issuance the moment tokens were configured. A key
  shared with the skill-grant tokens would also let a leak on either server forge the
  other's tokens, and would make an object token and a skill-grant token verify under one
  root. And a raw key in the environment is inherited by every child process, while this
  repository's other private key is named by a path (`MESHYCHAT_JWT_PRIVATE_KEY_PEM_PATH`,
  `apps/authapi/config.py:6`, `lines 136`). The file is created exclusively, the rule `23ca808`
  applied to key files. Hex is the stored representation. The key file's format is recorded
  as divergence (d) in 5.3.
- `MESHY_CAPABILITY_ROOT_PUBKEYS`: the VERIFY set, `kid=hex;kid=hex`. A process that only
  verifies needs no private key. The active kid's public key must be in it or boot fails.
- The authority block carries `cap_version(1)` and the allow policy REQUIRES it (4.1), so a
  token minted under any key for any other purpose never matches an allow.
- The operator populates these from the secret store; the code stays store-agnostic.
- A dev-only ephemeral key is allowed only behind an explicit flag, and a test fails if that
  flag is read outside
  `DEBUG`.

**Boot is fail-closed.** With tokens enabled (`MESHY_CAPABILITIES=on`), a missing or invalid key, a verify set
that does not contain the active kid, or an `import biscuit_auth` that fails (1.1) REFUSES TO
START. V1 rule A4: a gate that silently turns itself off is the defect class
(`docs/V1-DESIGN-RULES.md:38-42`). With tokens disabled the module is not imported at all,
so today's 3.14 deploys are unaffected. **The cost 034 already carries applies here too
(critique C15):** `ready()` runs for every `manage.py` command as well as for daphne
(`apps/memoryskills/apps.py:9-12`), so with tokens ON a broken key file also stops
`bootstrap_owner`, `set_owner_tier` and every other command. The runbook line is: unset
`MESHY_CAPABILITIES` to run a command while the key is being repaired.

**Rotation.** From day one with a kid, because the Python API already has the provider
(1.3 rows 16-17):

1. generate a new keypair and kid; 2. add its public key to `MESHY_CAPABILITY_ROOT_PUBKEYS`
on every verifier; 3. flip `MESHY_CAPABILITY_ROOT_KEY_PATH`/`_KID` on the minter; 4. old
tokens keep verifying until they expire (the authority TTL cap, 2.3); 5. drop the old public
key. Every step takes effect only when every process that verifies has RESTARTED, because the
set is read once at startup (critique C21).
**Emergency** (suspected leak): drop the old public key and restart every verifier. Every
token under it dies (row 17) and issuers re-mint. Because a root leak lets an attacker mint
ANY token, the second line is the ISSUANCE REGISTER of 2.5 (c):
a token is honoured only if this server's mint route recorded its authority revocation id.
The first version named the live issuer check as the second line. It is not one: a forger
names a REAL admin of the namespace as issuer and passes it (critique C3). With the register
a root-key thief also needs a write to the database.

### 2.3 The authority block (block 0, signed by the root)

Minted by the server from a validated request; every term is a PARAMETER, never spliced
into the Datalog text (the spike's f-string for constant prefixes is not a pattern to copy).

```
cap_version(1);
issuer({issuer_peer});                 // uuid string, lowercase hyphenated
subject({subject_peer});
right({ns}, {prefix}, {action});       // repeated; ONE namespace per token; prefix "" = all of it
depth_cap({n});                        // 0..4
check if time($t), $t < {expires_at};  // mandatory; expires_at - now <= 24 h at mint
```

UUIDs are rendered as strings because a UUID value would become a bytes term and break string
equality.
The 24-hour ceiling is a recommendation (open question Q3).

**One namespace per token (critique C9).** The first version allowed `right` facts over
several namespaces, which made "the issuer holds admin on every namespace in the token" a
mint-time loop and let an admin of ONE namespace revoke (2.5 (a)) a token that also carried
authority over another. A token names one namespace; a peer that needs two holds two tokens.
The mint route refuses a second namespace.

### 2.4 Attenuation blocks

Two kinds, and the difference is the whole security argument:

**Restriction block (first-party, keyless).** Anyone holding the token may append one.
It carries checks only; any facts it states are inert (row 9). Typical content:

```
check if resource($ns, $r), $ns == "shared/wiki", $r.starts_with("topic/auth/");
check if action("read");
check if operation($op), ["memory.read", "memory.query"].contains($op);
check if time($t), $t < {sooner};
```

It never changes who may present the token, which is why it needs no identity.

**Delegation block (third-party, signed by the current holder).** The only way the holder
changes. The delegator signs with its own registered peer key (`peer_keys.public_key`, a
TEXT column holding the base64url of the raw 32-byte Ed25519 key, `sql/002_peer_keys.sql:7`,
decoded as `fetch_caller_pubkey` does, `apps/memoryskills/permissions.py:70-71`; biscuit
renders it `ed25519/<hex>`, row 1.3 header):

```
delegate({executor_peer});
delegation_expires({x});               // x - now <= 300 s at every use
check if time($t), $t < {x};
// plus any restriction checks, exactly as above
```

Host rules, fail-closed, in this order (the walk in 1.3, plus two the spike did not need):
the external key must be a LIVE key of the current holder by the same grace-aware rule
provenance uses (`apps/memoryskills/permissions.py:58-65`: `active` or inside the revocation
grace window); it must not be one of the eight small-order points
(`apps/crypto/primitives.py:41-52`), a one-line set lookup kept as defence in depth even
though biscuit's own block-signature check IS strict (section 7, Q7); exactly one `delegate`/`delegation_expires` pair under
that key; at most 300 s of life left; no repeated peer; hops at most `min(depth_cap, 4)`;
the total block count at most 10 (a size bound, independent of the depth cap).

**What the 300 s bound is, exactly (critique C12).** The server never sees a delegation block
being made, so it cannot bound a hop's lifetime from its creation.
What it enforces is that a presented hop expires within 300 s of NOW. A hop signed with a
one-hour expiry is refused for its first 55 minutes and honoured in its last five: its usable
window is still at most 300 s, but it can be post-dated. The liveness checks run at every use
(2.5 (b)), so post-dating cannot outlive a revocation or a suspension; this is written down so
nobody reads the constant as the issuance-time rule.

**The peer key is reused, and the reuse is domain-separated (MEASURED, critique C17).** A
delegation block is signed with the same key that signs envelopes. biscuit-auth 6.0.0's normal
parse (`SerializedBiscuit::from_slice`) uses `ThirdPartyVerificationMode::PreviousSignatureHashing`, where
the external signature covers `\0EXTERNAL\0\0VERSION\0`, the version, `\0PAYLOAD\0`, the block
and `\0PREVSIG\0` with the previous block's signature. No JCS
envelope (it begins with `{`) and no DPoP JWS (ASCII) can equal that, and the previous
signature binds the block to its place, so it cannot be lifted onto another token. The legacy
mode without it (`unsafe_from_slice`) must never be reachable.
That the Python binding's `Biscuit.from_base64` reaches `from_slice` and not the legacy path
is INFERRED from its API (rival: it calls the legacy path); T15 decides it.

### 2.5 Revocation

(a) **Explicit.** A table of revoked block ids, checked on every use:

```
capability_revocation (
  revocation_id   BYTEA PRIMARY KEY,        -- 64 bytes, one block's id
  revoked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  revoked_by_peer UUID NOT NULL REFERENCES peers(id),
  reason          TEXT NOT NULL,
  not_after       TIMESTAMPTZ NOT NULL      -- the token's own expiry, for a later prune decision
)
```

RLS like the three memory tables (`server_internal` only, `apps/memoryskills/db.py:9-11`),
with its write policies in the shape divergence 029 now requires of every table
(`sql_meshychat/012`): an INSERT policy bound to `server_internal` and NO UPDATE or DELETE
policy, so append-only is the database's property, not the code's (critique C18).
Rows are never deleted in v1: the table grows by one row per revoke, and a prune can be
decided later from `not_after`, once the row count says it matters. The record of who
revoked what lives in the append-only event log below, which no retention job may touch
(V1 rule A1, `docs/V1-DESIGN-RULES.md:16-23`). Revoking the authority id kills the token and every
descendant (1.2); revoking a delegation block's id kills that branch.

**Who may revoke:** the issuer; any live admin of the token's one namespace; and any peer that signed a
delegation block in the presented token, for its own block and below (it proves which block
by presenting the token; the server computes the ids). Every revoke also appends to an
issuance/revocation event log, append-only, which is where the audit lives.

(b) **Implicit, and the reason the token is not the whole truth.** The token contributes
provenance and availability, while the LIVE row stays authoritative for everything mutable. On every use,
ONE SQL statement confirms

1. no revocation id of the presented token is in the table (a);
2. the token's authority revocation id IS in the issuance register (c);
3. the issuer still holds `admin` on the request's namespace by THE predicate
   (`meshy_namespace_permission`, 3.3) AND is itself `status = 'active'`. The status half is
   added by the critique (C6): `check_permission` never reads the peer's own status
   (`apps/memoryskills/permissions.py:95-125`), so a suspended namespace owner still holds
   implicit admin by it, and every token that owner issued would have lived on;
4. every peer in the holder chain is `status = 'active'`;
5. every delegation key is live by the grace-aware rule, and a key with NO row is DEAD.
   Divergence 030's `revocation.key_states` reads an unknown key as "not revoked" for a
   socket (`apps/wsgw/revocation.py:40-45`); for a capability, unknown refuses, so the two
   must not share that one decision;
6. for a `shared/*` namespace, the owner of every peer in the holder chain is dev-plane
   (the I2 grant half, section 4.2, critique C2).

So an issuer who loses admin or is suspended, a suspended delegator, a rotated-out
delegation key and a hand-off to a product-plane peer all end the capability without anyone
touching the revocation table.

(c) **The issuance register (critique C3).** The append-only issuance event the mint route
already writes (3.1: issuer, subject, namespace, rights, depth cap, expiry, kid and the
authority revocation id, never the token) is also a LOOKUP: clause 2 above requires the
presented token's authority id to be in it. That makes a root-key leak insufficient on its
own (2.2), costs nothing a use does not already pay (it is the same statement), and gives the
revoke route one place to find a token's issuer. It takes the 029 write shape of (a).

### 2.6 Open sockets, and where this meets divergence 030 (STATUS B15)

**What divergence 030 built, on main** (`apps/wsgw/revocation.py:1-45`; STATUS B15, landed
as `1da6f3b`, its review applied in `820ddfb`): a LISTEN on the NOTIFY channel
`meshy_peer_revoked` (`apps/wsgw/constants.py:99`) whose payload is a POINTER
(`{"peer_id", optional "namespace"/"kid"}`), plus a timed sweep; on either, it RE-READS
standing from the tables and closes sockets for three causes with codes 4410 (peer
inactive), 4411 (read on shared/mesh-chat revoked), 4412 (the key the JWT's `cnf` names
revoked) (`apps/wsgw/constants.py:137-139`). `memory.grant {revoke}` sends the NOTIFY inside
its own transaction (`apps/memoryskills/handlers.py:527`), so a rolled-back revoke never
closes anything. Its read test, `has_mesh_read`, calls
`check_permission(MESH_CHAT_NAMESPACE, peer_id, "read", conn)`
(`apps/wsgw/revocation.py:175-191`, the call at `lines 188`): site 10 of 3.4.

**The review of `c766b76` changed one rule this design inherits.** The grant cause now fires
only on a TRANSITION this process saw (the socket held read when it connected, or a sweep saw
it holding read), never because a notification named the namespace
(`apps/wsgw/revocation.py:31-40`). A capability cause must take the same shape: the evidence
that a socket held a capability is what the connection recorded at its upgrade, never a
payload.

**How the two meet, in order:**

1. **v1 accepts no token on the WebSocket.** A socket's authority stays the DB grant it had
   at upgrade, so 030's three causes are complete for v1 and nothing in this design changes
   them. A `Meshychat-Capability` header on an upgrade is REFUSED (slice 1), never ignored.
2. **The one hard dependency, now on main:** when slice 2 (section 6.2) makes the resource
   a REQUIRED argument, `has_mesh_read` passes `resource=NAMESPACE_WIDE`. The first version
   said "whichever branch lands second fixes it"; 030 has landed, so slice 2 carries it and
   T2 turns red if it does not.
3. **When tokens reach the socket (a later slice, not v1):** the connection records the
   token's revocation ids, holder chain and expiry beside the `cnf` key it already records;
   a revoke `pg_notify`s the SAME channel with the SAME pointer shape plus
   `"revocation_id"`; the listener re-reads `capability_revocation` and the 2.5 (b) liveness
   and closes with a fourth cause, **4413 capability revoked or expired** (unused on main,
   checked); the sweep also closes on token expiry. The pointer-not-content rule (NOTIFY is
   unauthenticated, `apps/wsgw/revocation.py:26-29`) carries over unchanged: a forged
   notification can only make the server re-read, never close a socket in good standing.
   **Before 4413 exists, that slice must choose a transport (critique C7).** The socket
   carries its credentials in `Sec-WebSocket-Protocol` entries, not headers, because a
   browser cannot set headers (`apps/wsgw/auth.py:4-8`). A token of several KiB in that one
   header is unmeasured against the server's total-header limit (3.1).

---

## 3. Presentation and verification

### 3.1 How a node presents a token

- **HTTP header `Meshychat-Capability: <biscuit, url-safe base64>`**, at most **8 KiB**. One
  header for every HTTP surface. The first version said 16 KiB, which the served stack cannot
  deliver (critique C7, MEASURED by reading the installed source). daphne serves through
  Twisted's `HTTPChannel`, whose `totalHeadersSize = 16384` bounds the request line plus ALL
  headers together (`twisted/web/http.py:2319`, enforced at `lines 2367` as a bare 400 and a
  disconnect, Twisted 26.4.0); uvicorn's h11 has a 16 KiB default for the same thing. A
  16 KiB token plus the JWT and the DPoP proof never reaches Django, so no typed code is
  returned. 8 KiB leaves room for both. The spike's 3-block token was 1416 characters, so the
  10-block cap (2.4) should keep a legitimate token far below 8 KiB (INFERRED from one
  3-block measurement; T20 pins the largest token the mint route can produce).
- **A request that carries the header MUST carry a valid DPoP proof, whatever its method.**
  Reads are bearer-only on purpose (`apps/peers/auth.py:34-36`, `lines 57-58`, `lines 150`), so a
  stolen bearer JWT reads. With a capability that is not good enough: the check becomes
  `if dpop_enforced() and (request.method in WRITE_METHODS or "Meshychat-Capability" in request.headers)`.
  Main already has the building block (critique C11): since divergence 027 a GET MAY carry
  a proof, checked by the write path's own `require_bound_proof` and spent
  (`apps/meshsem/views.py:135-156`, `_reader`), and `tools/meshy_node` sends one on its
  context and monitor GETs (`tools/meshy_node/monitor.py:862-864`). There a missing or bad
  proof degrades to "no possession"; with the header present it is a 403 instead. No client
  of THIS server sends the header today (its own transports hard-code `None`); for other
  clients that is inferred (8, inference 6).
- **On the signed-envelope path** (memory skills through
  `POST /v1/agents/{memory-peer}/skills/{name}/invoke`, `apps/calls/urls.py:51-52`, and A2A
  `message_send`, `apps/a2a/urls.py:59`), the SIGNED payload also carries
  `"capability_sha256": "<hex sha256 of the exact header string>"`. The server refuses a
  mismatch in either direction (header without the field, field without the header, wrong
  hash). The token itself is NOT put in the payload, because the payload is stored verbatim
  in `shared_memory.provenance_jsonb` (`apps/memoryskills/handlers.py:892-905`) and in
  `skill_calls.args`, where any reader of the namespace could copy it. The hash binds the
  token to one signed call without persisting it. The comparison lives in
  `_bind_envelope_to_the_call` (`apps/memoryskills/handlers.py:1400-1435`), which every
  `dispatch` caller passes through, so the board's in-process path
  (`apps/board/declare.py:306`), which passes no principal, refuses a payload that names a
  capability.
- **Mint and revoke** are `POST /v1/memory/capabilities` and
  `POST /v1/memory/capabilities/revoke`: POSTs, so DPoP is already enforced; the mint
  response carries the token once; an append-only event row records issuer, subject,
  namespace, rights, depth cap, expiry, kid and the authority revocation id, never the
  token, and that row is the issuance register of 2.5 (c). The mint route is itself a caller
  of the namespace predicate (issuer admin) and of the plane check (a product-plane subject
  on `shared/*` is refused, 4.2). A retried mint mints a second token; both are registered
  and each is revoked by its own authority id.

**Why a stolen token alone is useless** (measured in 1.3 rows 2, 6, 11, 14): the holder
walk ends at one peer id, and the caller must be that peer. The caller is the JWT subject
(`act.sub` when present, `apps/peers/auth.py:135-141`), the JWT carries `cnf`, and with the
header present the request must also carry a DPoP proof signed by the `cnf` key and bound to
this method, this path and the bearer JWT (its `ath`; NOT the capability), with a single-use
jti (`apps/peers/auth.py:186-224`). To use a stolen token an attacker needs the holder's JWT
AND the holder's DPoP private key. To use a stolen token as someone ELSE, they need the
holder's peer signing key to forge a delegation block (row 11).

**On the LAN this is the whole defence (critique C8).** Operator decision 6 accepts cleartext
on the LAN, bearer tokens included. A LAN observer has the JWT and the capability for free, and
holder binding reduces to the DPoP private key. The token's facts (namespace, prefixes,
peer ids) are readable by the same observer, because a biscuit is signed, not encrypted:
nothing secret may ever be a fact. Because the proof does not cover the header, an active
attacker on that LAN could also strip the header, or swap in another token the SAME holder
holds. Neither widens the holder's authority, but the request is then decided by an
authority the holder did not choose. The envelope path already binds the header
(`capability_sha256`); slice 7 binds it on GETs by requiring a `cap` claim (the header's
sha256) in the proof, an extra claim RFC 9449 permits (critique C11).

**Today's silent window, and slice 1 closes it on this server.** The server ignores an
unknown header today. A client that started sending `Meshychat-Capability` before the server
enforced it would be authorised by its DB grants and believe the token was in force. Slice 1
refuses the header with `E_CAPABILITY_UNAVAILABLE` while tokens are off. The refusal is a
MIDDLEWARE, not a per-view check (critique C16): every HTTP route and every WebSocket upgrade
refuses the header unless it is on an explicit accept list, and T16 walks the URL resolver
so a route added later cannot ignore it silently. A per-view check would have missed the
board's declare route, the mesh GETs and the upgrade.

**The slice 1 client rule (critique C8).** A client sends `Meshychat-Capability` only after the
server advertises `object-capability-v1` (Q11). `tools/meshy_node` gets that check with its
first token feature, so a header is never sent to a route that does not enforce it.

### 3.2 The verifier, in order

One function, `apps/memoryskills/capability.py: authorize_capability(conn, *, token,
caller_peer_id, namespace, resource, action, operation, now)`, called only from
`check_permission` (3.3); steps 1 and 3 are also run by the view before the call starts
(see "Where each refusal surfaces" below). Steps, each a typed refusal:

1. **Size and parse**: at most 8 KiB (3.1); `Biscuit.from_base64(token, provider)` with the
   kid provider. Failure is `E_CAPABILITY_INVALID`, never a 500 (divergence (b), 5.3).
2. **Cache lookup** (5.1) of the crypto-only result, keyed by `sha256(token)`.
3. **Holder walk** (1.3 and 2.4), yielding `(holder, chain peers, delegation keys, hops,
   revocation ids)`. `caller_peer_id != holder` is `E_CAPABILITY_HOLDER`.
4. **Liveness**, one SQL statement (2.5 (b)). Revoked is `E_CAPABILITY_REVOKED`; a plane
   refusal is `E_PRODUCT_NO_SHARED_GRANT` with its audit row (4.2); every other clause is
   `E_CAPABILITY_DENIED` with the reason.
5. **Datalog** with the host facts (section 4.1) and explicit limits: 1000 facts, 100
   iterations, 100 ms (1.3 row 20). A failed check or no
   matching allow is `E_CAPABILITY_DENIED`; a run-limit error is `E_CAPABILITY_DENIED` too
   (fail closed) and is counted, because a legitimate decision should never reach it.
6. **Return a decision record** `{"via": "capability", "cap_id": <authority id hex>,
   "issuer", "holder_chain", "hops"}`.

The new codes join `VALID_ERROR_CODES` (`apps/memoryskills/errors.py:54-58`), or the handler
seam would coerce them to `E_INTERNAL` (`apps/memoryskills/errors.py:65`), registered the way
divergence 028 registered its four (`apps/memoryskills/errors.py:45-52`).

**Where each refusal surfaces (critique C10).** The first version promised a 403. On the
envelope path a handler refusal is not an HTTP status: it becomes a signed `failed` skill
result, written to `skill_calls.error_code` and `error_message` and returned in a 200
terminal body when the call is sync, or read later by `GET /v1/calls/{id}` when it is not
(`apps/calls/route.py:177-200`, `lines 283-288`; `apps/calls/views.py:454-465`).
So steps 1 and 3 (parse, signatures, holder) ALSO run in the view, before `start_call`,
where a failure IS an HTTP 403 and creates no `skill_calls` row; the view keeps only the
parsed result for the handler. Steps 4 and 5 (liveness, the per-object decision) stay in
`check_permission` and surface as a signed failed result with `E_CAPABILITY_REVOKED` or
`E_CAPABILITY_DENIED`. Because those messages are persisted, no refusal text may contain the
token or any prefix of it (T14).

### 3.3 `check_permission` gains a key

Today: `check_permission(namespace, peer_id, need, conn)` (`apps/memoryskills/permissions.py:79-125`),
namespace owner then one grant row, no key anywhere.

Proposed:

```python
def check_permission(namespace, peer_id, need, conn, *, resource, principal=None):
    """resource: Key("k") for a point op, Prefix("p") for a range op, or NAMESPACE_WIDE.
    REQUIRED and keyword-only: a call site that does not say what it touches is a
    TypeError, so no caller can be left behind silently.
    principal: None (DB grants decide, exactly today's behaviour) or a PresentedCapability.
    """
```

- `principal is None`: today's predicate, unchanged, except that it moves into ONE SQL
  function (`meshy_namespace_permission(namespace, peer) -> text`) so the re-expressions in
  3.5 can call it instead of copying it. That is V1 rule A2's "one predicate, implemented as
  a database function, called by every layer and re-expressed by none"
  (`docs/V1-DESIGN-RULES.md:25-30`). Behaviour must be identical. The first version said the
  existing tests prove it; that is a PREDICTION about code not written (critique C13), and it
  holds only if the tests cover the predicate's edges: the owner's implicit admin, the NEWEST
  grant row deciding (`ORDER BY created_at DESC LIMIT 1`, `apps/memoryskills/permissions.py:112`,
  so a newer lower grant beats an older higher one), an expired row and an unclaimed
  namespace. Slice 3 adds a table test over those four before the move and proves it RED
  against a deliberately wrong function.
- `principal` present: section 4.2's rule; `authorize_capability` decides.

The `principal` travels from the transport: the view reads the header and builds a
`PresentedCapability(token, sha256)`; the pipeline carries it in a NEW field,
`StartCallInput.object_capability`; `route._execute` passes it to `handlers.dispatch`, which
passes it to the handler. The handler signature grows one keyword, `principal=None`. Every
new call is terminal inside `start_call` (divergence 017, `apps/calls/pipeline.py:280-284`),
so the token never has to outlive its request. If a queued or resumed execution path is ever
added, it must refuse a token-bearing call rather than persist the token for later.

**Not the existing `capability_token` field.** `StartCallInput.capability_token`
(`apps/calls/pipeline.py:108`, hard-coded `None` at `apps/calls/views.py:429` and
`apps/a2a/views.py:508`) is the SKILL-grant gate 1: `authorize()` hands any non-None
value to `capability.verify_grant_biscuit` (`apps/calls/authorize.py:215-219`), which raises
`BiscuitMintingNotConfigured`, a `NotImplementedError` (`apps/calls/capability.py:34-51`).
Routing an object token through it would turn every token-bearing memory call into an
internal error, and would later make an object token answer a skill-grant question it does
not encode. The two tokens may share the key-store CODE, never a key (C1; open question Q5).

### 3.4 Every call site, found by tracing

Two instruments, because one could miss what the other sees:

- **Static:** an AST walk (scratch `trace_refs.py`) that follows every binding of the
  function: `from .permissions import check_permission`, the `apps/memoryskills/__init__.py:29`
  re-export, `import ... as` module aliases, `x = check_permission` assignments, the name
  passed as a value, and the name as a string. It prints CALL or VALUE and the enclosing
  function.
- **Dynamic:** `sys.monitoring` PY_START on `check_permission.__code__` and
  `handlers.dispatch.__code__` during a full run of the suite (scratch `trace_plugin.py`),
  recording the calling frame. An alias, a `getattr`, a callback or a function passed as a
  value cannot hide from the code object. Result in 3.4.1.

**Production call sites of `check_permission` on main at `0b6767f` (static, all CALL).**
Re-traced by the critique with `git grep` over every import form: the package re-export
(`apps/memoryskills/__init__.py:29`), `from .permissions import`, and the function-local
imports the 027, 030 and 033 code uses to avoid import cycles. The first version's table had
nine rows at c5457ac; main has eleven (critique C5).

| # | site | enclosing | need today | resource to pass | token accepted |
|---|---|---|---|---|---|
| 1 | `apps/memoryskills/handlers.py:512` | `handle_grant` | admin | `NAMESPACE_WIDE` | **no** (4.3) |
| 2 | `apps/memoryskills/handlers.py:793-794` | `handle_write` | write, or admin when `pre_discipline` | `Key(key)` | write only; never for `pre_discipline` |
| 3 | `apps/memoryskills/handlers.py:987` | `handle_read` | read | `Key(key)` | yes |
| 4 | `apps/memoryskills/handlers.py:1125` | `handle_query` | read | `Prefix(key_prefix or "")` | yes |
| 5 | `apps/memoryskills/handlers.py:1253` | `handle_list` (namespace given) | read | `Prefix(prefix or "")` | yes |
| 6 | `apps/memoryskills/handlers.py:1319` | `handle_prune` | admin | `Key(key)` or `NAMESPACE_WIDE` | **no** |
| 7 | `apps/mesh/views.py:110` | `require_mesh_read`, from the `_route` wrapper at `lines 125` AND, new with divergence 031, from `_mesh_read_refusal` at `lines 291-300` (the addressed `/v1/mesh/wait`) | read on shared/mesh-chat | `NAMESPACE_WIDE` | later slice (6.2, slice 7); never for the wait in v1 |
| 8 | `apps/meshsem/views.py:74` | `_route.wrapper` | read on shared/mesh-chat | `NAMESPACE_WIDE` | later slice |
| 9 | `apps/wsgw/mesh_notify.py:468` | `resolve_recipients` | read on shared/mesh-chat | `Key(nudge.key)` | no in v1 (no socket tokens, 2.6) |
| 10 | `apps/wsgw/revocation.py:188` | `has_mesh_read` (divergence 030, on main) | read on shared/mesh-chat | `NAMESPACE_WIDE` | no in v1 |
| 11 | `apps/meshsem/search.py:781` | `_anchor_body_withheld` (divergence 027), exported as `body_withheld` (`lines 790`) and reached by the 033 push preview (`apps/wsgw/mesh_notify.py:476-505`, `preview_gate`) and the addressed wait's preview | read on shared/mesh-chat | **`Key(anchor key)` / `Key(nudge.key)`**, never namespace-wide: it answers whether memory.read would show THIS key | **no, by stated decision** |

**Site 11 is a SIMULATION of memory.read, and a token breaks its invariant (critique C5).**
027 and 033 show a body or a preview "exactly when memory.read would" show it, by calling
the same gates memory.read's invoke path calls (`apps/meshsem/search.py:754-784`,
`apps/calls/pipeline.py` `refusal_without_envelope`). Its signature today is
`(conn, reader)`, with no key. Two consequences for this design:

- slice 2 adds the key to `body_withheld` and its callers, so the required `resource=` is the
  row's key; a namespace-wide stand-in would be wrong the day `object_ownership` (Q4) or a
  key-scoped read decision exists;
- once tokens exist, memory.read WITH a token can show a key that site 11, which has no
  token, withholds. That is the fail-closed direction and v1 keeps it, but the docstrings and
  divergences 027 and 033 must then say "exactly when memory.read without a capability
  would", or they state something false. T21 pins it.

Test call sites (they move with the signature): `tests/test_memory_skills.py:721`,
`tests/test_mesh_graphread.py:259`, `tests/test_mesh_graphread.py:687`.

**How requests reach sites 1 to 6.** `handlers.dispatch` (`apps/memoryskills/handlers.py:1438-1444`,
a dict at `lines 1389-1397`) has exactly two production callers:
`apps/calls/route.py:188` (`_execute`, from `route` at `lines 149`, from
`apps/calls/pipeline.py:285`, from `start_call`, which `apps/calls/views.py:450` `invoke`
and `apps/a2a/views.py:525` `message_send` call) and `apps/board/declare.py:306`
(`declare`, from `apps/board/views.py:211` `declare_act` and `declare_in_process` at
`apps/board/declare.py:310-327`, which the CLI `apps/board/__main__.py` reaches). The
handlers are otherwise referenced only as dict values (`apps/memoryskills/handlers.py:1390-1396`)
and `__all__` strings. `namespace.claim` calls no `check_permission` (bootstrap authority
only, `apps/memoryskills/handlers.py:410`).

#### 3.4.1 Dynamic trace

**Historical, @c5457ac, and NOT re-run on main (critique C5, C13).** The critique did not
re-run this trace: its instruments are scratch-only and the box allows two suite runs per
agent. The static re-trace above found the two sites main added (10 and 11). Slice 2 re-runs
the dynamic trace on its own base before it claims completeness, and T2 is the standing
guard. What follows is the original record.

The full suite ran at c5457ac (this branch, before the commit that adds this file) with the
tracer loaded: **`1206 passed, 4 skipped, 2 warnings in 1002.86s (0:16:42)`, exit 0**, the
same count `fin/status` records for c5457ac. Every production site c5457ac had (sites 1 to
9 of the table above, at the c5457ac lines the appendix lists) was EXECUTED, and nothing
else was: the dynamic set equals the static set, row for
row, and so do the three test sites. `dispatch` was entered from exactly the two production
callers named below and from tests. The recorded rows are in the appendix.

What that does and does not prove. It proves the static walk missed no caller the suite
reaches, and the tracer is its own positive control (it recorded all twelve sites the static
walk predicted). It cannot see a caller no test reaches; the static walk is what covers
those, and it found none. Sites 10 and 11 are absent from both because neither existed at
c5457ac.

### 3.5 The predicate re-expressed in SQL (not calls, same decision)

These decide "may this peer read this namespace" without calling `check_permission`, so a
call-site trace cannot find them; a search for the grant table's predicate did. Each is an
A2 violation today and each must either call the SQL function from 3.3 or stay DB-grant-only
by stated decision:

| site | what it decides | v1 decision |
|---|---|---|
| `apps/memoryskills/handlers.py:1183-1210` `_list_visible_namespaces` (the grant clause at `lines 1194`) | `memory.list` with no namespace | call `meshy_namespace_permission`; a presented token is `E_CAPABILITY_NOT_ACCEPTED` here (tokens authorise objects, not enumeration) |
| `apps/mesh/wait.py:347-362` `_PEER_VIS_CTE`, used by the plain wait and, since divergence 031, by the addressed scan (`lines 552-566`) | `/v1/mesh/wait` visibility | call the SQL function; tokens not accepted in v1 |
| `apps/dashboard/tenant_scope.py:65-190` (CTEs reading the grant table at `lines 72`, `lines 102`, `lines 136`, `lines 178`), `apps/dashboard/queries.py:447-456` | dashboard OWNER visibility, keyed on `grantee_owner_id` and an owner session | out of scope: an owner-session surface, not a peer capability surface. Named so nobody assumes it is covered |

---

## 4. Policy

### 4.1 Host facts (the authorizer's, never the token's)

```
time({now});                   // server clock, whole seconds
caller({caller_peer});         // the authenticated peer (act.sub or sub)
operation({skill});            // "memory.read" | "memory.query" | "memory.list" | "memory.write" | "mesh.read"
resource({ns}, {r});           // r = the key (point) or the requested prefix (range), "" = whole namespace
action({a});                   // "read" | "write"
allow if cap_version(1), right($ns, $p, $a), resource($ns, $r), $r.starts_with($p), action($a);
deny if true;
```

`resource` merges the spike's `namespace` and `key` facts, so a holder's restriction check
names the namespace and the key in one fact. `cap_version(1)` is an authority fact, which the
allow policy trusts by default; requiring it means a token minted for another purpose never
matches (2.2, C1). The allow is the ONLY policy that can pass;
every block's checks must also hold. Range semantics: a range request is allowed iff its
prefix lies inside a granted prefix, so `memory.query` with no `key_prefix` needs a
namespace-wide right. The SQL side agrees because both handlers escape LIKE metacharacters
before appending `%` (`apps/memoryskills/handlers.py:1080`, `lines 1228`), making the SQL filter a
literal prefix match (INFERRED equal to `starts_with`; rival: a non-deterministic collation
on `key` would make LIKE and codepoint prefix differ. Test T9 pins it).

### 4.2 The rule when a token and a DB grant both apply

**A presented token is the whole authority for that request.** If the header is present the
token decides, conjoined with the live issuer check (2.5 (b)). If the token fails, the
request is REFUSED; it never falls through to the caller's own grants. If there is no
header, DB grants decide exactly as today.

Why not "either one": a peer acting as an executor on a narrowed capability would silently
exercise its own broader grant the moment the token refused, which is the confused-deputy
shape a capability exists to prevent, and the audit record would name an authority the
caller did not invoke. The current gate treats verification failure as a deny rather than a
fall-through (`apps/calls/capability.py:25-26`).

Why not "both": requiring the executor to hold its own grant too would make delegation to a
peer without a grant impossible, which is most of what delegation is for.

**The layers that stay independent (unchanged by any token):**

- **Skill layer** (`skill_grants`, `apps/calls/authorize.py:193-236`). A memory call is a
  call to the memory authority peer; a cross-tenant executor still needs same-tenant/dev
  group or an active `skill_grants` row for the memory skill. A namespace token cannot fix
  a skill denial, as today a grant cannot fix a plane denial
  (`apps/memoryskills/permissions.py:8-18`).
- **Plane gates, BOTH halves of I2 (critique C2).** The write half,
  `E_PRODUCT_NO_SHARED_WRITE` in `handle_write` (`apps/memoryskills/handlers.py:804-812`),
  a token cannot pass, because it runs on the caller's own owner after the permission check.
  The first version kept only that half. The GRANT half, `E_PRODUCT_NO_SHARED_GRANT`
  (`apps/memoryskills/handlers.py:559-567`), forbids a product-plane peer from holding ANY
  grant on `shared/*`, read included, and a token is a grant the server never sees being
  passed on: an admin could mint one for a product-plane subject, and any holder could
  delegate one offline to a product-plane peer, who would then READ `shared/*`. So the grant
  half is re-checked at every USE (2.5 (b) clause 6): for a `shared/*` namespace every peer in
  the holder chain must be dev-plane, else `E_PRODUCT_NO_SHARED_GRANT` with the same
  `audit_tenancy_denial` row (`apps/memoryskills/handlers.py:596-601`). The mint route refuses
  a product-plane subject the same way. I1 (a cross-subtree grant must be bounded, `lines 568-574`)
  holds by construction, because every token expires.
- **Authorship, divergence 028, is not delegable (critique C4).** R1 to R4
  (`apps/memoryskills/handlers.py:305-370`) and the database trigger
  (`sql_meshychat/011_mesh_message_single_writer.sql`) bind a mesh message to the peer that
  signed it. A token lets an executor write AS ITSELF: E holding A's write token on
  `shared/mesh-chat` cannot write a later version of A's message (R4, and 011's trigger for
  any other path), and `value.from` must still be one of E's labels (R2). T17 pins that a
  token changes neither.
- **Envelope binding** (`apps/memoryskills/handlers.py:1400-1435`) and signature verification.

### 4.3 Per operation

| operation | action | resource fact | token? | why |
|---|---|---|---|---|
| `memory.read` | read | key | yes | point read |
| `memory.query` | read | `key_prefix` or "" | yes | range; `contains`, `tags`, `since` filter INSIDE the authorised range |
| `memory.list` (namespace) | read | `prefix` or "" | yes | range |
| `memory.list` (no namespace) | - | - | **no** | enumerates DB relationships (3.5) |
| `memory.write` | write | key | yes | point write |
| `memory.write` on a `shared/mesh-chat` `msg/` key | write | key | yes | as the executor itself only; 028's R1 to R4 still apply (4.2) |
| `memory.write` `pre_discipline` | admin | - | **no** | a custody write is administrative (`apps/memoryskills/handlers.py:791-794`) |
| `memory.grant` | admin | - | **no** | a token must never mint DURABLE authority: a 300 s delegate turning itself into a permanent grant row is an escalation |
| `memory.prune` | admin | - | **no** | destructive and bulk |
| `namespace.claim` | - | - | **no** | bootstrap authority (MUST-1) |
| mesh GET routes (`apps/mesh/urls.py:17-21`: inbox, owed, threads, drift, neighbours), `/v1/mesh/search`, `/context` | read | `shared/mesh-chat`, "" | later slice | they read the whole namespace graph. When one accepts a token, a `related_to` edge (divergence 032) is returned only if BOTH its ends lie inside the token's scope, or the edge discloses that an out-of-scope message exists and resembles one in scope (critique C20) |
| `/v1/mesh/wait`, plain and `addressed=1` (031), WS push and its preview (033) | read | - | **no** in v1 | 3.5, 2.6, site 11 in 3.4 |
| `POST /v1/board/acts` (`apps/board/urls.py:20`) and `declare_in_process` | write | - | **no** | its dispatch carries no principal (3.4); the header is refused by slice 1's middleware |

A token presented where the table says no is `E_CAPABILITY_NOT_ACCEPTED`, never silently
ignored.

### 4.4 Worked example: a delegation chain

The namespace admin `A` wants worker `E` to read and write one task subtree, and `E` hands a
read-only slice of it to reviewer `F` for five minutes.

1. `A` mints (`POST /v1/memory/capabilities`, DPoP, A holds admin on `ns/tasks`):

```
cap_version(1); issuer("A"); subject("A");
right("ns/tasks", "task/42/", "read"); right("ns/tasks", "task/42/", "write");
depth_cap(2);
check if time($t), $t < 2026-09-24T12:00:00Z;
```

2. `A` delegates to `E`: a third-party block signed with A's peer key:

```
delegate("E"); delegation_expires(2026-09-23T12:05:00Z);
check if time($t), $t < 2026-09-23T12:05:00Z;
```

3. `E` delegates a read-only sub-slice to `F`, signed with E's key:

```
delegate("F"); delegation_expires(2026-09-23T12:04:00Z);
check if time($t), $t < 2026-09-23T12:04:00Z;
check if action("read");
check if resource($ns, $r), $r.starts_with("task/42/review/");
```

4. `F` calls `memory.read` on `ns/tasks` key `task/42/review/notes` with the header, a DPoP
   proof and `capability_sha256` in the signed payload.

Verification: parse under the kid's root key; the walk reads block 1 under A's key (holder
A, the subject) giving E, block 2 under E's key giving F, 2 hops, within `depth_cap(2)`;
caller F equals the holder; the SQL finds no revoked id, the authority id in the issuance
register, A still admin on `ns/tasks` and active, A, E and F active, A's and E's signing keys
live (`ns/tasks` is not `shared/*`, so no plane clause applies); the authorizer has
`resource("ns/tasks", "task/42/review/notes")`, `action("read")`, `right(... "task/42/",
"read")` matches, both of F's checks hold. **ALLOW.** Spike rows 3 and 7 are this shape.

The same token with `action("write")` fails the `check if action("read")` (row 4). With key
`task/42/other` it fails the prefix check (row 8). Presented by E, it fails the walk (holder
is F, row 6). Presented by F after 12:04 it fails the hop's time check.

### 4.5 Worked example: a denied over-attenuation

Take the chain spike 1.3 built for rows 3 to 5: `A` delegated to `E` with
`check if action("read")` and `check if resource($ns, $r), $r.starts_with("task/42/")` in
A's signed block. `E` now tries to give `F` more than E has:

```
delegate("F"); delegation_expires(...);
right("ns/tasks", "", "write");            // assert a wider right
right("ns/tasks", "", "admin");
check if resource($ns, $r), $r.starts_with("task/");   // a wider prefix than task/42/
```

- The asserted `right` facts are in a non-authority block. The allow policy trusts only the
  authority block and the authorizer, so they match nothing: `memory.write` still fails the
  `check if action("read")` in A's block (the output's "Check n°2", A's block's third
  check), and `admin` matches no allow at all and lands on `deny if true` (row 9, both
  halves).
- The wider prefix is only another CHECK, and A's `task/42/` check still applies, so
  `task/7/x` is refused ("Check n°1", row 10).
- If E instead tries to extend the life to an hour, the host refuses the hop (row 12); if it
  tries a sixth hop, the depth cap does (row 13). If the thief `T` signs a block naming
  itself, the walk refuses it (row 11).

---

## 5. Cost, failure modes, divergence

### 5.1 Performance

MEASURED on HQD (8 logical CPUs, other workflows running, so the p99s are noisy), 3.13.12,
spike 2: parse plus signature check 288 us p50 for a 1-block, 356-character token and
1137 us p50 for a 3-block, 1416-character one; the whole decision including a fresh parse,
the walk and the authorizer, 758 us (depth 0) and 2370 us (depth 2) p50; the authorizer
alone on an already-parsed token, 326 us p50.

INFERRED: the cost grows with the number of blocks, one signature verification each (rival:
the growth is the third-party blocks' larger encoding or query cost, not signature count;
the spike does not separate them). The design does not depend on which.

**Caching.** Per process, keyed by `sha256(token)`: the parsed `Biscuit`, the walk's
result and the revocation ids, LRU of 1024 entries, entry life `min(60 s, earliest
expiry)`, the whole cache dropped when the root verify set changes (which, read once at
startup, means a restart). NOT cached: the
liveness SQL (it is the revocation mechanism) and the authorizer run (its facts differ per
request). With a warm cache a request pays the authorizer (about 0.3 ms p50 here) plus one
SQL statement, which replaces the one or two statements `check_permission` runs today
(`apps/memoryskills/permissions.py:95-115`). No negative caching: an invalid token costs a
parse each time, bounded by the 8 KiB and 10-block limits and by B12's per-peer rate limit
when that lands.

### 5.2 Failure modes

| failure | behaviour |
|---|---|
| tokens enabled, key or verify set missing or invalid, or `biscuit_auth` import fails | refuse to start (2.2) |
| tokens disabled, header present, on ANY route or WebSocket upgrade | `E_CAPABILITY_UNAVAILABLE` from the middleware, never ignored (3.1, T16) |
| tokens enabled, header on a route not on the accept list | `E_CAPABILITY_NOT_ACCEPTED` (4.3, T16) |
| header larger than the server's total-header budget | a bare 400 from Twisted before Django runs (3.1); prevented by the 8 KiB cap, which the client also enforces before sending (T20) |
| malformed token, foreign root, dropped kid, tampered | `E_CAPABILITY_INVALID`, 403 |
| Datalog run limit | deny and count; limits explicit on every authorizer (1.3 row 20) |
| clock skew | the server's clock decides; whole-second, strict `<`, fails closed at a fractional boundary |
| revoked, issuer demoted, ISSUER SUSPENDED, delegator suspended, delegation key rotated out or unknown | denied on the next request by the liveness SQL (2.5 (b)); open sockets per 2.6 |
| token signed by a leaked root key but never minted by this server | denied: its authority id is not in the issuance register (2.5 (c), T19) |
| token delegated to a product-plane peer on `shared/*` | `E_PRODUCT_NO_SHARED_GRANT` and an `audit_tenancy_denial` row (4.2, T18) |
| tokens enabled and the key file or verify set broken | the server AND every `manage.py` command refuse to start (2.2) |
| a refusal message quotes the token | prevented by construction and pinned by T14, because refusal messages are persisted in `skill_calls` (3.2) |
| replay dedup crosses the authorised key | `_existing_by_dedup` looks up by payload hash OR call_id across ALL namespaces and returns that row as the replay (`apps/memoryskills/handlers.py:636-651`, used at `lines 797` and `lines 951`). Under namespace grants that is a correctness wrinkle; under a KEY-scoped authority it answers with another key's namespace, key and version. INFERRED low (critique C14): it needs a foreign row's call_id or its exact payload hash, and the invoke path's own idempotency check keys on the caller (`apps/calls/pipeline.py`, step 2); the board's in-process path has no such row. Slice 4 still constrains the lookup to the authorised `(namespace, key)` or refuses a hit outside it |
| Datalog injection | every term is a parameter (1.2 shows the dict form); a review check and test T10 |
| token copied out of provenance or `skill_calls` | not possible: only its hash is stored (3.1) |

### 5.3 Decisions recorded here

| # | decision | why |
|---|---|---|
| (a) | Object rights are `right(ns, prefix, action)` and are checked at `check_permission`. | #913/#523 are about objects; skill-grant tokens and gate 1 (`apps/calls/authorize.py:215-219`) are a later, separate slice. |
| (b) | A malformed credential is 403 `E_CAPABILITY_INVALID`. | It is the caller's error, and a 500 invites retries. |
| (c) | An enabled capability gate refuses to boot with an invalid root key. | V1 rule A4. |
| (d) | Root keys use hex. | The stored format is explicit so it is not changed accidentally. |
| (e) | Delegation uses third-party token blocks, with a 4-hop and 300-second limit. | It is offline and attenuable, with no row per hop. |
| (f) | Rotation by kid is available from day one. | The Python provider exists (1.3 rows 16-17). |
| (g) | A GET carrying `Meshychat-Capability` must carry DPoP. | Holder binding needs proof of possession on reads too. |
| (h) | Object tokens use `MESHY_CAPABILITY_ROOT_KEY_PATH`, a file-backed key distinct from `MESHYCHAT_BISCUIT_ROOT_SK`. | C1: reusing that name makes `issue_grant` refuse every grant, and a shared key couples token forgery. |
| (i) | The live statement requires the token to have been minted here through the issuance register. | C3: otherwise a leaked root can forge tokens naming a real admin. |

**Adjacent, not fixed here:** this service's skill-grant `authorize` does not walk a delegated
grant's parent chain at call time (`apps/calls/authorize.py:145-178` reads only the row). A
delegated `skill_grants` row therefore outlives a revoked parent until its own expiry, which is at most 300 s
(`apps/grants/delegation.py:101-113`). Bounded, and worth its own STATUS row.

---

## 6. Test plan and build order

### 6.1 Tests, each written RED first against the code as it stands

Every negative carries its positive control in the same test, the shape
`tests/test_write_side_ownership.py` uses, because "refused" is also what a broken gate
looks like.

- **T1 (the #913 shape, the first red):** with namespace write, a token scoped to
  `task/42/` writes `task/42/a` (control) and is refused on `task/7/a`. Red today because
  the header is ignored and both writes land.
- **T2 (call-site completeness):** import every module that references `check_permission`
  and call each site with a fixture; any call without `resource=` fails. Red when a site is
  missed. On main that is eleven sites, including 030's `has_mesh_read` and 027's
  `_anchor_body_withheld` (3.4).
- **T3 (holder binding):** A's token presented by T is refused; presented by A it passes.
- **T4 (delegation chain):** 4.4's chain allows, and each of its four variants is refused.
- **T5 (over-attenuation):** 4.5's block is refused on write, admin and the wider prefix,
  with the parent token as the control.
- **T6 (no fall-through):** a caller holding namespace-wide write presents a read-only token
  and writes: refused (and the same write without the header succeeds).
- **T7 (liveness):** revoke by id (authority, then a mid-chain block), demote the issuer,
  SUSPEND the issuer while its grant row stays live (the case `check_permission` alone
  passes), suspend the delegator, rotate the delegation key past grace, delete the key's row:
  each refuses, and an unrelated token keeps working.
- **T8 (DPoP on GET):** a header on a GET without DPoP is refused; with DPoP it passes; with
  a proof whose `cap` claim names a different token, refused (slice 7).
- **T9 (range semantics):** `memory.query` with a prefix inside, equal to, and outside the
  granted prefix, plus a key containing `%` and `_`, against the real LIKE.
- **T10 (injection):** a namespace or key containing `"`, `)`, `;` and a Datalog keyword
  round-trips as a single term.
- **T11 (fail-closed boot):** enabled with each of the three missing or broken inputs:
  startup refuses.
- **T12 (interop):** both supported verification paths accept the fixture corpus and a
  Python-minted token. This turns inference 1 in section 8 into a measurement.
- **T13 (limits):** a fact bomb over 1000 facts is refused by the deterministic bound.
- **T14 (the token is never persisted or echoed):** drive every refusal path (invalid,
  holder, revoked, denied, not accepted, unavailable), then scan `skill_calls.args`,
  `result`, `error_message`, `shared_memory.provenance_jsonb`, the issuance and revocation
  rows, the response bodies and the captured log for the token and for its first 16
  characters. Positive control: plant the token in one message on purpose and prove the scan
  finds it.
- **T15 (a block cannot be lifted):** a delegation block cut from token X and appended to
  token Y with the same holder is refused; a third-party block in the legacy (v0) signature
  format is refused. Settles inference 7 in section 8.
- **T16 (the header on every route):** walk `django.urls.get_resolver()`; every route not on
  the accept list answers a request carrying the header with `E_CAPABILITY_UNAVAILABLE`
  (tokens off) or `E_CAPABILITY_NOT_ACCEPTED` (on), and so does a WebSocket upgrade. Positive
  control: an accept-list route with a valid token is allowed. A route added later fails the
  walk until someone decides for it.
- **T17 (authorship is not delegated):** E holding A's write token on `shared/mesh-chat`
  writing version 2 of A's `msg/` key gets `E_NOT_MESSAGE_AUTHOR`; with `value.from` naming A,
  `E_FROM_MISMATCH`; E's own new message under the same token lands (control).
- **T18 (plane at use):** a dev-plane holder delegates read on `shared/wiki` to a
  product-plane peer, who is refused with `E_PRODUCT_NO_SHARED_GRANT` and an audit row; the
  same hand-off to a dev-plane peer reads (control); a mint naming a product-plane subject on
  `shared/*` is refused.
- **T19 (issuance register):** a token signed with the real root key in the test but never
  minted through the route is refused, including one naming the namespace's real owner as
  issuer; after the test inserts its register row it is allowed (control).
- **T20 (header budget):** the largest token the mint route can produce, delegated to the
  10-block cap, plus a real JWT and DPoP proof, is served by daphne and decided by the view;
  the client refuses to send one over 8 KiB.
- **T21 (site 11 stays tokenless):** a reader whose only authority is a token is withheld
  the anchor body and the preview (fail closed), and the docstrings say "without a
  capability".
- **Mutation list for the slice reviews:** drop the holder comparison; read `delegate`
  without `trusting`; skip the liveness SQL; let a failed token fall through; accept `admin`
  in a token; drop the issuance-register clause; drop the issuer-status clause; drop the
  `shared/*` plane clause; treat an unknown delegation key as live; drop `cap_version(1)`
  from the allow. Each must turn a named test red.

### 6.2 Build order, each slice landable on its own

Numbering, for whoever integrates: the divergence this records takes the next free number at
integration, 037 or later (035 is reserved for ops-livez and 036 for ops-replay); its
migrations take the next free `sql_meshychat` number, 014 or later (011 to 013 are taken on
main).

0. **Runtime and record decisions (operator, S).** Pin the server to CPython 3.13 or build a
   wheel of biscuit-python against a PyO3 that supports 3.14 (Q1). Revise `README.md:82` and
   an internal project document (not part of this release), which still record tokens as dropped (Q9). Choose the
   key's name and custody (Q10). No code until these are decided.
1. **Refuse the header (S).** A middleware: while tokens are off, `Meshychat-Capability` on any
   route or upgrade is `E_CAPABILITY_UNAVAILABLE`. Closes the silent window on this server
   (3.1). T16. No dependency.
2. **`resource=` on `check_permission` (S-M).** Required keyword, all eleven sites in 3.4
   pass one, behaviour identical; `body_withheld` and its callers gain the key. T2, and the
   dynamic trace re-run on this slice's base.
3. **One SQL predicate (M).** `meshy_namespace_permission`; `check_permission` and the two
   re-expressions in 3.5 call it. The four-edge table test of 3.3 first, RED against a wrong
   function. This is A2's database function.
4. **Verifier, dark (M).** Key store (a file, read once in `ready()`), `authorize_capability`,
   cache, the revocation table and the issuance register with 029's write shape, the six
   liveness clauses of 2.5 (b), the dedup scoping of 5.2. Behind `MESHY_CAPABILITIES`,
   default off; T3, T5, T10, T11, T13, T14, T15, T18, T19.
5. **Mint and revoke routes (M).** Admin-gated, one namespace, product-plane subject refused
   on `shared/*`, DPoP, token returned once and registered. T7, T19.
6. **Envelope path on (M).** Header plus `capability_sha256` through invoke and A2A to
   sites 1 to 6, the view's pre-check of 3.2; T1, T4, T6, T9, T17, T20. The `meshy_node`
   advertisement check (Q11) ships in the same slice as its first token feature. T12 before
   this slice is called done.
7. **Mesh GET routes (S).** DPoP with a `cap` claim when the header is present, sites 7 and
   8, `related_to` edges filtered to the token's scope; T8.
8. **Sockets (M, later).** Transport decision first (2.6 step 3), then 4413 on 030's
   listener.

---

## 7. Open questions, with a recommendation each

The session that wrote this was asked to put questions to the operator through a question
tool; that tool was not available to it, so the questions are here instead. The critique ran
under the same instruction and the same limitation: it had no question tool either, so its
new questions (Q9 to Q12) are here too, each with a recommendation.

- **Q1. Python runtime.** 0.4.0 has no 3.14 build (1.1). **Recommend** pinning the server to
  CPython 3.13 (the cp313 wheel is published for Windows, macOS and manylinux, and the
  spike ran on it) and adding a `python_requires` so the pin is stated, not remembered.
  The alternative, a private wheel from a patched source, adds a supply-chain step nobody
  owns. Critique's measurement (1.1): main byte-compiles on 3.13.12; the suite and the
  dependency install on 3.13 are not yet run, so the pin is a slice-0 task with its own
  green run, not a setting.
- **Q2. Who may mint.** **Recommend** namespace admins only in v1 (2.2). A chain creator
  minting exact-key tokens for its own chain is useful for claim rows but widens the mint
  surface; revisit after T1 is green.
- **Q3. Maximum authority TTL.** **Recommend** 24 hours, re-mintable, because the live checks
  make a long token safe to revoke but a short one bounds a root leak.
- **Q4. Does this close #913 on the DEFAULT path?** No, and it should not pretend to. A token
  only narrows a caller who presents one; a peer with namespace write and no token can still
  append to another peer's chain. **Recommend** a per-namespace, opt-in `object_ownership`
  mode in the SQL predicate (append to an existing chain only as its creator, an admin, or
  under a token naming the key), turned ON for namespaces used as locks and OFF for
  `shared/wiki`, whose confirm flow REQUIRES a different peer to append a version
  (`apps/memoryskills/handlers.py:178-190`; `sql_meshychat/011_mesh_message_single_writer.sql:8-10`
  says the same). **Revised by the critique (C4):** mesh-chat authorship is no longer B01's
  open branch, it is main (divergence 028: R1 to R4 plus `sql_meshychat/011`), and 011 is
  the one place main already enforces row ownership where V1 rule A2 asks for it, "at the
  storage boundary, not in views" (`docs/V1-DESIGN-RULES.md:29-30`). So `object_ownership`
  should GENERALISE 011, a per-namespace flag on `shared_memory_namespace` read by a BEFORE
  INSERT trigger, rather than live inside the permission predicate, which is still a check
  in the application.
- **Q5. Skill-grant biscuits (the gate 1).** **Recommend** leaving
  `apps/calls/capability.py` raising until the object slices are proven; then port
  the `grant(...)` token with T12 as its test, sharing the key-store CODE but under
  its own key (C1), and only after `issue_grant`'s refusal on `MESHYCHAT_BISCUIT_ROOT_SK`
  (`apps/grants/service.py:267-268`) is replaced by real minting.
- **Q6. The `authz` record on the wire.** **Recommend** adding `authz` to the handler result
  and `capability` to write provenance ONLY when a token was used, so every no-token
  response stays byte-identical to the daemon's.
- **Q7. Is biscuit's Ed25519 check strict? ANSWERED while writing this, so no longer
  open.** Yes: biscuit-auth 6.0.0 verifies every block signature strictly, the same strictness
  `apps/crypto/primitives.py:78-96` insists on. **Recommend** keeping the host-side
  small-order refusal of 2.4 anyway, because it costs a set lookup and does not depend on a
  third-party crate staying strict across upgrades.
- **Q8. Revocation table retention.** **Recommend** never pruning in v1 (2.5 (a)); decide
  from `not_after` when the row count says it matters.
- **Q9. Is biscuit still "dropped by decision"? (critique)** `README.md:82` and
  an internal project document (not part of this release) on main say so; this document rests on a relayed
  operator choice that reverses it. **Recommend** the operator confirm the reversal in words
  that slice 0 copies into both files, so the repository stops contradicting its own design.
- **Q10. The key's name and custody (critique C1).** **Recommend** a distinct key named by a
  path, `MESHY_CAPABILITY_ROOT_KEY_PATH`, never the `MESHYCHAT_BISCUIT_ROOT_SK`: that name
  switches off skill-grant issuance here, and a shared key couples the two servers'
  forgeries. The key file sits under the same OS account as the node-chat server, which on HQD
  is the account every seat's session runs under; that is no worse than the JWT signing key,
  which already lets its holder be any peer, but it is not better either.
- **Q11. How a client learns a server takes tokens (critique C8).** **Recommend** a field in
  `/v1/health` (`"capabilities": ["object-capability-v1"]`) that `tools/meshy_node` reads
  before it ever sends the header, and refuses to send otherwise. the server, which
  production runs on a VPS host until the cutover, never advertises it, so no token is ever
  silently ignored there.
- **Q12. Tokens on the mesh GET routes at all? (critique C20)** The graph routes read the
  whole namespace, and 032's inferred `related_to` edges, which the operator keeps ON as hints
  through the cutover (`cutover-list.md`, decision 15), relate messages across it. **Recommend**
  keeping slice 7 out of v1 until a caller needs a key-scoped view of the graph; when it
  lands, return an edge only when both its ends are in scope.

---

## 8. Measured and inferred, in one place

**MEASURED**

1. biscuit-python 0.4.0 is the newest release; wheels cp37 to cp313, none cp314 (PyPI, 1.1).
2. It fails to install on CPython 3.14.3 both ways; it installs on 3.13.12 (1.1).
3. It bundles biscuit-auth 6.0.0 (sdist build log).
4. Mint, keyless attenuation, offline verify and revocation ids work, with a control (1.2).
5. Third-party delegation, holder binding, over-attenuation refusal, depth and TTL caps,
   kid rotation, foreign-root and tamper refusal (1.3 rows 1-19).
6. The 1 ms default time limit trips under load; 100 ms does not (1.3 row 20).
7. Per-request cost on HQD under load (1.3 rows 21-25).
8. The call sites of `check_permission` and `dispatch` (3.4, static and dynamic).
9. Today's gaps, re-read on main: no key (`permissions.py:79`), reads not DPoP-gated
   (`auth.py:150`; an OPTIONAL proof on the mesh GETs since 027), `capability_token=None`
   hard-coded (`calls/views.py:429`, `a2a/views.py:508`), the predicate copied into SQL
   (3.5), cross-key dedup (`handlers.py:636-651`).
10. The revocation listener, channel, close codes and grant-revoke NOTIFY, on main as
    divergence 030 (2.6).
11. biscuit-auth 6.0.0 checks block signatures strictly, and its default limits are 1000 facts
    and 100 iterations (Q7, 1.3 row 20).
12. (critique) Setting `MESHYCHAT_BISCUIT_ROOT_SK` makes `issue_grant` refuse every grant
    (`apps/grants/service.py:267-268`, `tests/test_grants.py:1330-1336`).
13. (critique) Twisted 26.4.0, which daphne serves through, bounds the request line plus all
    headers at 16384 bytes (`twisted/web/http.py:2319`, `lines 2367`).
14. (critique) biscuit-auth 6.0.0's normal parse verifies third-party signatures over a
    domain-separated payload that includes the previous signature.
15. (critique) All 303 `.py` files of main at `0b6767f` byte-compile on CPython 3.13.12; the
    newest biscuit-python is still 0.4.0 with no cp314 wheel (PyPI, 2026-09-24).
16. (critique) The call-site set on main: eleven production sites of `check_permission`
    (3.4), by `git grep` over every import form. Static only.

**INFERRED, each with the rival the evidence does not exclude**

1. Tokens produced through the supported APIs interoperate. Rival: the binding converts a term
   differently (a string against a bytes term, a date's precision), so identical-looking tokens
   disagree. T12 decides it.
2. The cp313 wheel is built from the same source whose sdist log was read. Rival: the wheel
   was built from a different lock. Low stakes; `pip show` plus the T12 run settle it.
3. Verify cost scales with signature count. Rival: with encoding or query size. Neither
   changes the design.
4. The measured costs are representative. Rival: HQD was loaded by parallel workflows and
   the numbers are high, or a production box is slower; one box, one run.
5. LIKE-prefix and `starts_with` agree. Rival: a non-deterministic collation. T9.
6. No client sends `Meshychat-Capability` today. MEASURED for this server's own transports
   (`None` is hard-coded); for third-party clients it is inferred. Rival: some client sends
   one and is silently ignored now, which is why slice 1 comes first.
7. (critique) The Python binding's `Biscuit.from_base64` reaches biscuit-auth's safe
   `from_slice`, not the legacy `unsafe_from_slice`. Rival: it calls the legacy path, and a
   lifted or v0-format third-party block verifies. T15.
8. (critique) Moving the predicate into SQL keeps behaviour identical. Rival: an edge the
   current tests do not cover (the newest-row rule, an expired row) changes. The table test
   of 3.3.
9. (critique) The operator chose Biscuit. Relayed to this document; the repository still
   says "dropped by decision". Rival: the choice was narrower (a design, not a build). Q9.
10. (critique) The dedup oracle of 5.2 is low risk because it needs a known foreign call_id
    or payload hash. Rival: call_ids leak somewhere a key-scoped holder can read them (a
    mesh message that quotes one). Slice 4 scopes the lookup either way.
11. (critique) A 10-block token stays far below 8 KiB. Rival: large right sets or long
    prefixes push it over. T20.

---

## Appendix: the dynamic trace (@c5457ac, historical)

`sys.monitoring` PY_START on the two code objects, caller frame recorded, full suite at
c5457ac, pytest exit status 0. The file:line columns are c5457ac's; section 3.4 has main's.
Columns: target, calling file:line, calling function, number of executions in the run.

```
check_permission  apps/memoryskills/handlers.py:402   handle_grant        15
check_permission  apps/memoryskills/handlers.py:672   handle_write       102
check_permission  apps/memoryskills/handlers.py:852   handle_read         17
check_permission  apps/memoryskills/handlers.py:990   handle_query        11
check_permission  apps/memoryskills/handlers.py:1118  handle_list          4
check_permission  apps/memoryskills/handlers.py:1177  handle_prune         4
check_permission  apps/mesh/views.py:98               require_mesh_read   64
check_permission  apps/meshsem/views.py:61            wrapper             47
check_permission  apps/wsgw/mesh_notify.py:302        resolve_recipients  24
check_permission  tests/test_memory_skills.py:721     test_a_product_plane_caller_cannot_write_a_shared_namespace  1
check_permission  tests/test_mesh_graphread.py:259    test_the_gate_is_memory_reads_own_function  1
check_permission  tests/test_mesh_graphread.py:687    test_the_visibility_query_agrees_with_check_permission_on_every_grant_state  8
dispatch          apps/board/declare.py:300           declare             13
dispatch          apps/calls/route.py:188             _execute            23
dispatch          tests/test_bootstrap_dev_plane.py:110   _call            6
dispatch          tests/test_envelope_binding.py:46..113  (7 tests)        7
dispatch          tests/test_memory_skills.py:143     _call              170
dispatch          tests/test_memory_skills.py:1159    test_limits_clamp_and_bad_arguments_are_refused  1
```

The instruments, kept out of the repository (scratch only): `trace_refs.py` (the static
alias-following AST walk) and `trace_plugin.py` (the pytest plugin). The spikes,
`spike20.py` and `spike_chain.py`, ran in throwaway virtual environments under the same
scratch directory; nothing was installed into the project's `.venv`.

---

## Critique log

An adversarial review of this document against origin/main `0b6767f7`, 2026-09-24, by a seat
that did not write it. The design was written against `c5457ac`; main has since gained the
read path and authorship binding (027, 028, `sql_meshychat/011`), the security wave (029 with
`sql_meshychat/012`, 030's revocation closes, one JWT verifier, one owner session), the
addressed wait (031), `related_to` edges (032, `sql_meshychat/013`), frame role and preview
(033) and the roster loader with `signed_by` (034). The review read those files at
`0b6767f`, not the older copies.

**Starting state.** `git status` in the worktree was clean: there was no uncommitted revision
by the designer to keep or discard. The branch (one commit) rebased onto `0b6767f` without
conflict. That commit's `Co-Authored-By` and `Claude-Session` trailers were removed while
rebasing, under the operator's ruling on trailers in this repository; its author and body are
unchanged.

**What was kept, because it held up.** The no-fall-through rule (4.2) and its reasons; holder
binding through third-party blocks signed by the delegator's registered key, with the host
walk reading `delegate` only under that key; `admin` never mintable, and no token for
`memory.grant`, `memory.prune`, custody writes or `namespace.claim`; the token kept out of
every signed payload, with only its hash bound; explicit authorizer limits; refusing the header while tokens are off; the
required keyword `resource=` with a completeness test; keeping the object token out of
the `capability_token` gate; day-one rotation by kid; the spikes themselves, whose strict
verification and default-limit claims hold.

### Citations checked (23), each against `0b6767f`

| cited (`@c5457ac`) | on main | verdict |
|---|---|---|
| `apps/memoryskills/permissions.py:79-125` `check_permission` | unchanged | correct; it never reads the peer's own status, which the design missed (C6) |
| `apps/memoryskills/permissions.py:58-65` grace-aware key rule | unchanged | correct |
| `apps/peers/auth.py:161` DPoP on writes only | `lines 150` | true; moved by the one-verifier change. Optional DPoP on GETs now exists (027) |
| `apps/peers/auth.py:146-151` `act.sub` | `lines 135-141` | true |
| `apps/peers/auth.py:197-234` bound proof | `lines 186-224` | moved; "bound to this method, path and token" is true only if "token" is the JWT (`ath`), not the capability (C11) |
| `apps/calls/pipeline.py:107` `capability_token` | `lines 108` | true |
| `apps/calls/views.py:429`, `apps/a2a/views.py:508` | unchanged | true |
| `apps/calls/authorize.py:215-219`, `apps/calls/capability.py:25-26`, `lines 34-51` | unchanged | true |
| `apps/grants/config.py:42-46` | `lines 46-50` | true, and incomplete: setting that variable makes `issue_grant` refuse (C1) |
| `apps/memoryskills/errors.py:45`, `lines 55-56` | `lines 54-58`, `lines 65` | true |
| `apps/memoryskills/handlers.py:521-534` dedup across namespaces | `lines 636-651` | true; severity stated too high (C14) |
| `apps/memoryskills/handlers.py:756-770` provenance | `lines 892-905` | true |
| `apps/memoryskills/handlers.py:686-690` I2 write half | `lines 804-812` | true, and only half of I2 (C2) |
| `apps/memoryskills/handlers.py:163-166` wiki confirm | `lines 178-190` | the lines are `_chain_root_author`; the claim is better cited to 011's header, now added |
| `sql/002_peer_keys.sql:3-12` "a raw 32-byte key" | unchanged | wrong: `public_key` is TEXT holding base64url (C19) |
| `apps/crypto/primitives.py:41-52`, `lines 78-96` | unchanged | true |
| `apps/memoryskills/db.py:9-11` | unchanged | true; the new tables also need 029's write shape (C18) |
| `apps/mesh/wait.py:300-315` `_PEER_VIS_CTE` | `lines 347-362` | true; now also used by 031's addressed scan |
| `docs/V1-DESIGN-RULES.md:16-23`, `lines 25-30`, `lines 38-42`; an internal project document (not part of this release) | unchanged | true |
| `sec:apps/wsgw/revocation.py:182`, `sec:...constants.py:99`, `lines 137-139` | `apps/wsgw/revocation.py:188`; constants unchanged | true, on main as 030; the transition rule of `820ddfb` added |
| 3.4's nine production sites | eleven | stale (C5) |

### Findings, most severe first

| id | severity | finding | what changed |
|---|---|---|---|
| C1 | HIGH | Reusing the `MESHYCHAT_BISCUIT_ROOT_SK` breaks existing behaviour: on main, setting it makes `issue_grant` refuse every skill grant (`apps/grants/service.py:267-268`, pinned by `tests/test_grants.py:1330-1336`). One root for object and skill-grant tokens couples their forgery; a raw key in the environment is inherited by child processes, where this repo's other private key is a path | 2.2: `MESHY_CAPABILITY_ROOT_KEY_PATH` (a file, created exclusively, read once in `ready()`) and `MESHY_CAPABILITY_ROOT_PUBKEYS`; `cap_version(1)` required by the allow (4.1); divergence (h); Q5, Q10 |
| C2 | HIGH | Tenancy bypass. The design kept only I2's WRITE half. I2's GRANT half forbids a product-plane peer holding ANY grant on `shared/*`, read included (`handlers.py:559-567`). A token minted for, or delegated offline to, a product-plane peer would let it READ `shared/*`, and the server never sees an offline hand-off | 2.5 (b) clause 6 re-checks the plane of every chain peer at USE, with the same audit row; the mint route refuses a product-plane subject; 4.2 rewritten; T18 |
| C3 | HIGH | Overclaimed mitigation. "The live issuer check is the second line" against a root leak does nothing: a forger names a REAL admin as issuer and passes it | 2.5 (c): the issuance event becomes a register the liveness statement requires; 2.2 rewritten; divergence (i); T19 |
| C4 | MEDIUM | The design predates 028. Mesh authorship is bound on main (R1 to R4, `sql_meshychat/011`), and nothing said a token must not delegate it; Q4 recommended ownership inside the permission predicate, while 011 is main's working instance of A2's "at the storage boundary" | 4.2 new layer (authorship not delegable), 4.3 row, T17; Q4 revised to generalise 011's trigger |
| C5 | MEDIUM | The call-site table is stale: main has eleven production sites, not nine. Site 11 (027's anchor gate, reused by 033's preview and the addressed wait) SIMULATES memory.read keylessly; with tokens its "exactly when memory.read would" stops being true | 3.4 rewritten for main; site 11 takes `Key(...)` and stays tokenless by decision, with its docstrings to change; 3.4.1 marked historical; T2, T21 |
| C6 | MEDIUM | A suspended issuer's tokens live on: `check_permission` reads no peer status, so a suspended namespace owner keeps implicit admin by it. And 030's `key_states` reads an UNKNOWN key as not revoked, which must not carry over | 2.5 (b) clauses 3 and 5; T7 extended |
| C7 | MEDIUM | The 16 KiB header cap cannot be delivered: daphne serves through Twisted, whose 16384-byte limit covers the request line plus ALL headers, and answers a bare 400 before Django runs. The WebSocket carries credentials in `Sec-WebSocket-Protocol`, which the socket slice ignored | 8 KiB cap (3.1), client-side enforcement, T20; 2.6 step 3 requires a transport decision before 4413 |
| C8 | MEDIUM | Coexistence downgrade the design did not see. Through the cutover the fleet server at `ab7032cc2e9d` ignores the header, and the fleet carries the server address in four places; a misrouted token is silently replaced by DB grants. The LAN is cleartext by operator decision 6, so the token and its facts are visible to any LAN host | 3.1: client-side advertisement check (Q11), the LAN paragraph, "nothing secret is a fact"; 5.2 row |
| C10 | MEDIUM | "E_CAPABILITY_INVALID (403)" is wrong on the envelope path: a handler refusal is a signed failed result in `skill_calls`, returned in a 200 (sync) body, and its message is persisted | 3.2: crypto and holder checks also run in the view (HTTP 403, no `skill_calls` row); object decisions stay signed results; T14 forbids the token in any persisted or returned text |
| C9 | LOW | Multi-namespace tokens made the mint check a loop and let an admin of one namespace revoke authority over another | one namespace per token (2.3), "who may revoke" narrowed |
| C11 | LOW | The DPoP proof binds the JWT, not the capability; on a GET an active LAN attacker can strip or swap the header for another of the same holder's tokens | 3.1 wording corrected; slice 7 requires a `cap` claim in the proof; T8 extended; 027's optional GET proof named as the building block |
| C12 | LOW | "300 s per hop" is a usable-window bound, not a lifetime: a post-dated hop is refused until its last 300 s, then honoured | 2.4 states it exactly |
| C13 | LOW | Claims presented as measured that were not: "behaviour is identical and the existing tests prove it" (a prediction); the dynamic trace stated in the present tense after main moved; "nothing breaks" for the DPoP change stated flat; the operator's choice of Biscuit, relayed while the repository says dropped | 3.3 table test; 3.4.1 historical; 3.1 qualified; Q9; section 8 inferences 7 to 11 |
| C14 | LOW | The dedup oracle's severity was overstated: it needs a known foreign call_id or payload hash | 5.2 row annotated; the fix in slice 4 kept |
| C15 | LOW | Fail-closed boot in `ready()` also stops every `manage.py` command | 2.2 runbook line |
| C16 | LOW | Slice 1 did not say where the refusal lives; a per-view check misses the board, the mesh GETs and the upgrade | a middleware with an accept list; T16; 4.3 board row |
| C17 | INFO | Key reuse across envelopes and delegation blocks: MEASURED domain-separated; the legacy parse mode must stay unreachable | 2.4 paragraph; T15; inference 7 |
| C18 | LOW | New tables must take 029's write-policy shape; divergence and migration numbers moved | 2.5 (a); 6.2 numbering (divergence 037 or later, `sql_meshychat` 014 or later) |
| C19 | LOW | `peer_keys.public_key` is TEXT base64url, not raw bytes | 2.4 corrected |
| C20 | INFO | 032's inferred `related_to` edges, kept ON as hints through the cutover (operator decision 15), would leak across a token's scope on a future token-accepting mesh route | 4.3 row; Q12 |
| C21 | LOW | Rotation and the emergency drop take effect only after every verifier restarts, because the set is read once | 2.2 rotation steps |

**Note, 2026-09-27 (operator rulings), appended after the review:** the cutover no longer runs
an intermediate server on HQD. Production moves from a VPS host straight to this server on HQD,
so C8's "through the cutover" now means "until the cutover". There is no rollback path.

### Not done by this review, and why

- The dynamic trace was not re-run on main (instruments are scratch-only; two suite runs per
  agent on this box). The static re-trace found the two new sites; slice 2 re-runs it.
- No suite run on CPython 3.13 and no dependency install there; only the byte-compile.
- No spike was re-run: every change here is to the design's reasoning and citations, not to
  what the library does.
- No production data was read and nothing was sent to any model or web service except a
  read of PyPI's public release metadata for biscuit-python.
