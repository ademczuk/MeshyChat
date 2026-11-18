"""Every process-lifetime container a request can grow is BOUNDED, and says how (E09).

WHY. An unbounded dedup cache holding full envelopes for the life of the process ran out of
memory (docs/ISSUE-RESOLUTION-MAP.md, "daemon OOM"). The house rule
written against it, "bounded caches (LRU + TTL)", had no enforcement: nothing noticed a new
module-level dict that a request adds to and nothing removes from. This test is the
enforcement.

WHAT IT FINDS, by reading the source (ast), under apps/ and meshychat/:

  1. a module-level container (dict, list, set, OrderedDict, defaultdict, Counter, a deque
     without maxlen, or a literal or comprehension of one) that code INSIDE A FUNCTION adds
     to: a subscript store, or append/add/setdefault/update/insert/extend/appendleft. A
     table built at import and only read afterwards is not a cache and is not listed;
  2. the same inside a process-lifetime INSTANCE: a class of the module instantiated at
     module level (or assigned to a `global` inside a function), whose methods add to a
     container attribute (`self.x[k] = v`, `self.x.add(v)`), and a container in a class body
     its methods add to;
  3. an unbounded memo decorator anywhere: `functools.cache`, or `lru_cache` with
     maxsize=None, which is a dict that only grows.

WHAT IT REQUIRES. Every site found is in LEDGER with the kind of bound it has, and the kind
is checked against the source where the source can show it:

  * "lru": a size cap and its eviction are both in the code: `len(<the container>)` in the
    test of an `if` or `while`, and a popitem/pop/del/clear on it INSIDE that statement's
    body. An eviction anywhere else (a reset's clear()) does not count: it is not what holds
    the cap (review MG2, 2026-09-26). Time-to-live, where there is one, is in the reason (a
    structural test cannot see a clock comparison bind to an entry).
  * "per-connection": entries leave when their owner does: a pop/del/discard/remove/
    difference_update on it, or a rebuild of it inside a method.
  * "config-keyed": the KEYS come from a set the code or the operator fixes (declared
    providers, declared spaces, status codes, fixed names), never from a request. There is
    nothing structural to check, so the reason names the key domain, and a reviewer reads
    it; the test's part is that a new cache cannot arrive without that sentence.

And the ledger holds nothing the source no longer has: a stale entry fails, so the ledger
cannot rot into an allow-list for names that come back meaning something else.

THE DETECTOR IS SHOWN TO FIRE: the controls below run it on planted sources, the daemon-OOM
shape first. A structural test that cannot catch its own planted positive is not a test.

LIMITS, stated. Only containers named at module or class level, or held by an instance that
is, are seen; a cache captured in a closure, or held by an object passed around and never
named globally, is not. Growth through an alias (`d = _cache; d[k] = v`) is not seen either
(a field-name scan is blind to aliased writes).
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCANNED = [p for base in ("apps", "meshychat") for p in sorted((ROOT / base).rglob("*.py"))
           if "tests" not in p.relative_to(ROOT).parts]

CTORS = {"dict", "list", "set", "OrderedDict", "defaultdict", "deque", "Counter"}
GROW = {"append", "add", "setdefault", "update", "insert", "extend", "appendleft"}
EVICT = {"popitem", "pop", "clear", "discard", "remove", "difference_update", "popleft"}

#: site -> (kind, the bound in words). Sites are "path::NAME" (module level),
#: "path::INSTANCE.attr" (a process-lifetime instance) or "path::Class.attr" (class body).
LEDGER: dict[str, tuple[str, str]] = {
    "apps/peers/turn.py::_reports": (
        "lru", "turn report rate histories: TRACKED_MAX (4096) peers, at most four timestamps each; "
               "expire idle histories after 60 seconds; a full live table refuses new peers, never resets quotas"),
    # ---- lru: a size cap and an eviction in the code
    "apps/meshsem/ctxindex.py::_cache._shards": (
        "lru", "the context index's resident shards: at most CACHED_SHARDS_MAX shards and the configured budget "
               "in total (BUDGET_BYTES unless MESHY_CTX_INDEX_BUDGET_BYTES sets at least BUDGET_BYTES_MIN, "
               "divergence 108), least recently used evicted first; each shard also expires after "
               "SHARD_MAX_AGE_S"),
    "apps/meshsem/ctxindex.py::_shard_meta": (
        "lru", "the context index's cold-shard filter bounds (frozensets of srcs and projects plus time bounds): "
               "at most SHARD_META_MAX shards in LRU order, invalidated on row/update count changes and expiring "
               "after SHARD_MAX_AGE_S"),
    "apps/mesh/views.py::_recipients_seen": (
        "lru", "divergence 083's per-peer bound on the tokenless recipients route: RECIPIENTS_TRACKED_MAX "
               "peers; when full, idle ones are dropped and a new peer is refused, never the table cleared"),
    "apps/mesh/work.py::_held": (
        "lru", "on-demand fleet facts: HELD_MAX (4096) entries, least recently used evicted first; "
               "successful snapshots held for HOLD_SECONDS (60 s), failed reads never cached"),
    "apps/mesh/views.py::_owed_key_seen": (
        "lru", "the owed/key route's per-peer bound, the same as _recipients_seen's: RECIPIENTS_TRACKED_MAX "
               "peers; when full, idle ones are dropped and a new peer is refused, never the table cleared"),
    "apps/meshsum/window.py::_COUNTS": (
        "lru", "COUNTS_MAX entries, least recently used out (B8)"),
    "apps/mesh/wake.py::SUBSCRIBER._pending": (
        "lru", "E22's addressing queue: at most ADDRESSING_BACKLOG_MAX (key, author) pairs; "
               "past it the whole backlog is dropped and every addressed waiter woken, and "
               "the addressing thread drains it"),
    "apps/memoryskills/capability.py::_CACHE._entries": (
        "lru", "CACHE_SIZE verified tokens, least recently used out, each until its expiry"),
    "apps/meshsum/cache.py::CACHE._entries": (
        "lru", "MAX_ENTRIES model parts, least recently used out, each for TTL_S (B8)"),
    "apps/core/peer_limit.py::_last_logged": (
        "lru", "cleared whole past MAX_TRACKED peers; one entry per rate-limited peer"),
    "apps/core/peer_limit.py::_limiter._buckets": (
        "lru", "max_tracked buckets; idle full buckets evicted first, then refused"),
    "apps/core/mesh_unversioned.py::LIMITER._last": (
        "lru", "cleared whole past LOG_MAX_LABELS (256) caller labels; one entry per label whose "
               "memory.write wrote an unversioned mesh message under the transitional "
               "MESHY_MESH_ACCEPT_UNVERSIONED (divergence 011), each logged at most once per "
               "LOG_INTERVAL_S (integ/r16 gate, 2026-09-27)"),
    "apps/authapi/ratelimit.py::sig_auth_peer_limiter._buckets": (
        "lru", "MAX_TRACKED_KEYS buckets; idle ones evicted first, then refused"),
    "apps/authapi/ratelimit.py::sig_auth_ip_limiter._buckets": (
        "lru", "MAX_TRACKED_KEYS buckets; idle ones evicted first, then refused"),
    "apps/core/redeem_limit.py::_limiter4._buckets": (
        "lru", "max_tracked (MAX_TRACKED 10,000) IPv4 client addresses (and the one unknown "
               "bucket), in LRU order; a bucket idle its TTL (burst/refill, the time to "
               "refill) is dropped first, then a NEW key is refused (B16)"),
    "apps/core/redeem_limit.py::_limiter6._buckets": (
        "lru", "max_tracked (MAX_TRACKED 10,000) IPv6 client /48s, in LRU order; a bucket "
               "idle its TTL (burst/refill) is dropped first, then a NEW key is refused (B16, "
               "a table of its own so an IPv6 flood cannot refuse new IPv4 clients)"),
    # ---- per-connection: an entry leaves with the socket, call or flight that owns it
    "apps/wsgw/connection_manager.py::MANAGER._by_peer": (
        "per-connection", "one list per connected peer, popped when its last socket goes"),
    "apps/wsgw/presence.py::BACKEND._held": (
        "per-connection", "one held arrival or departure per peer (divergence 105), deleted "
                          "when it fires or is cancelled; at most the connected peers plus "
                          "those inside their 5 s grace"),
    "apps/wsgw/presence.py::BACKEND._shown": (
        "per-connection", "peers whose last frame was peer_online, discarded when their "
                          "peer_offline is sent; bounded like _by_peer plus the grace"),
    "apps/wsgw/call_registry.py::CALL_REGISTRY._by_call": (
        "per-connection", "registrations of live sockets, deleted on unsubscribe/close"),
    "apps/wsgw/call_registry.py::CALL_REGISTRY._by_connection": (
        "per-connection", "one set per live socket, popped when it closes"),
    "apps/wsgw/call_notify.py::CALL_BRIDGE._departed": (
        "per-connection", "sockets gone and not yet cleared; drained by the beat"),
    "apps/wsgw/call_notify.py::CALL_BRIDGE._urgent": (
        "per-connection", "swapped for an empty set on every drain"),
    "apps/wsgw/call_notify.py::CALL_BRIDGE._urgent_accepted": (
        "per-connection", "rebuilt without expired peers on every accept"),
    "apps/wsgw/call_notify.py::CALL_BRIDGE._lost_terminals": (
        "per-connection", "F04: one entry per local socket whose queue lost a terminal, deleted "
                          "when a pass takes its last name (every held name is taken by the "
                          "periodic sweep, and a gone socket's are let go); each entry holds at "
                          "most REPUSH_PENDING_MAX (256) call ids"),
    "apps/wsgw/mesh_notify.py::BRIDGE._readers": (
        "per-connection", "connected mesh readers, recomputed from the connected set"),
    "apps/meshsum/cache.py::CACHE._flights": (
        "per-connection", "one per in-flight model call, popped when it lands"),
    # integ/r14 composition (w2-mesh F12 beside w2-metrics E09, found by the r14 gate):
    "apps/mesh/wake.py::REGISTRY._waiters": (
        "per-connection", "one per parked /v1/mesh/wait request, discarded in _parked's finally; "
                          "at most MAX_WAITERS (512) at once: the view takes a slot "
                          "(try_acquire_waiter, else 503) before any park"),
    # ---- config-keyed: the key set is fixed by code or configuration, never by a request
    "apps/files/blob.py::_BACKENDS": (
        "config-keyed", "backend names registered by code at startup"),
    "apps/calls/chain_head.py::_ORPHANS._warned": (
        "config-keyed", "D08: one database time per entry of the COMMITTED known-orphans list "
                        "(chain_head_known_orphans.json, six entries); `due` refuses a key the "
                        "list does not hold (test_d08_due_refuses_an_anchor_the_list_"
                        "does_not_hold), so a frame or a request cannot add one"),
    "apps/meshsem/providers.py::_rerank_down": (
        "config-keyed", "(provider id, base_url) of DECLARED rerank providers"),
    "apps/meshsem/related_reads.py::_cov_cache": (
        "config-keyed", "(declared space, gate, calibration version); held COVERAGE_TTL_S"),
    "apps/mesh/graph.py::_coverage_held": (
        "config-keyed", "(host, port, database, namespace) of the serving connection: the views ask "
                        "only for MESH_CHAT_NAMESPACE, so one entry per database the process serves; "
                        "held COVERAGE_TTL_S (divergence 107)"),
    "apps/meshsem/ctxindex.py::_plans": (
        "config-keyed", "one shard plan per (declared embedding space, the configured context namespace); replaced, "
                        "never added to, when the space's chunk change counter (sql_meshychat/049) moves or PLAN_TTL_S passes"),
    "apps/meshsem/ctxjob.py::_yield_since": (
        "config-keyed", "one entry per embedding space the context index job walks (store.configured_space(), "
                        "one today); popped on the first tick the mail embedder is caught up"),
    "apps/meshsem/ctxjob.py::_deadline_stuck": (
        "config-keyed", "one counter per embedding space the job walks; popped by any tick that indexes or "
                        "keeps a vector"),
    "apps/meshsem/search.py::_indexes": (
        "config-keyed", "one per declared space (spaces.get refuses others); each index "
                        "capped at MESHY_SEMANTIC_MAX_VECTORS"),
    "apps/meshsem/search.py::_lexical": (
        "config-keyed", "one per declared space"),
    "apps/mesh/wake.py::SUBSCRIBER._stats": (
        "config-keyed", "a Counter keyed by the twelve event names wake.py passes to _count "
                        "(listens, notifications, probes, ...); never a request's value"),
    "apps/meshsem/search.py::_coverage": (
        "config-keyed", "(declared space, gate); held COVERAGE_TTL_S"),
    "apps/meshsum/pin.py::_cache": (
        "config-keyed", "(provider id, base_url) of declared chat providers; PIN_MAX_AGE_S"),
    "apps/meshsum/views.py::_cooldowns": (
        "config-keyed", "(provider id, base_url) of declared chat providers"),
    "apps/meshsum/views.py::_names_memo": (
        "config-keyed", "two fixed keys, 'at' and 'names'; names capped at PEER_NAMES_LIMIT"),
    "apps/core/metrics.py::HTTP_REQUESTS._counts": (
        "config-keyed", "HTTP status codes the server sends: at most the 100-999 range"),
    "apps/meshsum/record.py::_memo": (
        "config-keyed", "two fixed keys, 'stamp' and 'value'"),
    # Counter names, read from their callers 2026-09-26: literals, or built from code enums,
    # a call's status (a CHECKed column) and the channels this process LISTENs on (a
    # notification arrives only on a channel the connection listens to).
    "apps/wsgw/call_notify.py::CALL_BRIDGE._stats": (
        "config-keyed", "counter names: literals, call statuses, the two LISTEN channels"),
    "apps/wsgw/call_registry.py::CALL_REGISTRY._stats": (
        "config-keyed", "counter names passed as literals by the registry's callers"),
    "apps/wsgw/mesh_notify.py::BRIDGE._stats": (
        "config-keyed", "counter names: literals, code enums, the LISTEN channels"),
    # The request-path connection pool (apps/core/dbpool.py, off unless MESHYCHAT_DB_POOL_SIZE is set), read from
    # its code 2026-11-16 after the gate named these seven.
    "apps/core/dbpool.py::_idle": (
        "lru", "the configured pool size, itself at most MAX_POOL_SIZE (16): a session is kept only while "
               "len(_idle) < that size, and give_back trims the oldest past MAX_POOL_SIZE under the same lock; "
               "taken newest first, and closed when idle longer than MAX_IDLE_S"),
    "apps/core/dbpool.py::_parked": (
        "lru", "leases closed under a running statement, one per request that did so; the reaper (every "
               "REAP_EVERY_S while any is parked) ends the oldest running past MAX_PARKED (16) and forgets ended "
               "ones past MAX_PARKED after SETTLE_GRACE_S; and _park caps it AT INSERTION at MAX_PARKED * "
               "PARKED_HARD_X (64), the oldest ended and forgotten first, so no burst grows it past that"),
    "apps/core/dbpool.py::_stats": (
        "config-keyed", "twenty fixed counter names, all present from the start; every _count() call passes a "
                        "literal, and an unknown name raises KeyError instead of adding a key"),
    "apps/core/dbpool.py::_reaper": (
        "config-keyed", "one slot, assigned by index: the reaper thread or None"),
    "apps/core/dbpool.py::_canceller": (
        "config-keyed", "one slot, assigned by index: the cancel-sending thread or None"),
    "apps/core/dbpool.py::_last_log": (
        "config-keyed", "one slot, assigned by index: the time of the last stats line"),
    "apps/core/dbpool.py::_said": (
        "config-keyed", "warnings already logged once: 'driver', plus each distinct raw value the one setting "
                        "MESHYCHAT_DB_POOL_SIZE has held in this process (the environment of a running server "
                        "does not change, so one)"),
}


@dataclass
class Site:
    key: str
    line: int
    evicts: bool = False
    capped: bool = False
    guarded: bool = False       # an eviction INSIDE an if/while whose test is its len() cap


def _ctor(node) -> bool:
    if isinstance(node, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name in CTORS:
            return not (name == "deque" and any(k.arg == "maxlen" for k in node.keywords))
    return False


def _functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                        ast.Lambda))]


def _len_capped(node, match) -> set:
    """Site names whose `len()` is compared anywhere inside `node`."""
    capped = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Compare):
            for side in [n.left, *n.comparators]:
                if (isinstance(side, ast.Call) and getattr(side.func, "id", None) == "len"
                        and side.args and (s := match(side.args[0]))):
                    capped.add(s)
    return capped


def _ops(scopes, match) -> tuple[set, set, set, set]:
    """(grown, evicted, capped, guarded) site names, from code inside `scopes` (function
    nodes). `guarded`: an eviction sits in the body of an if/while whose TEST compares the
    site's len(), which is what keeps an LRU at its cap. An eviction elsewhere (a reset's
    clear(), a pop on another path) is an eviction, but not that one (review MG2,
    2026-09-26: a reset satisfied "lru" while the cap evicted nothing)."""
    grown, evicted, capped, guarded = set(), set(), set(), set()
    for scope in scopes:
        # A constructor's first assignment is the container being MADE, not rebuilt.
        rebuilds = getattr(scope, "name", None) != "__init__"
        for n in ast.walk(scope):
            if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in targets:
                    for e in (t.elts if isinstance(t, ast.Tuple) else [t]):
                        if isinstance(e, ast.Subscript) and (s := match(e.value)):
                            grown.add(s)
                        elif rebuilds and n.value is not None and (s := match(e)):
                            evicted.add(s)          # rebuilt wholesale inside a method
            elif isinstance(n, ast.Delete):
                for t in n.targets:
                    if isinstance(t, ast.Subscript) and (s := match(t.value)):
                        evicted.add(s)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if (s := match(n.func.value)):
                    if n.func.attr in GROW:
                        grown.add(s)
                    elif n.func.attr in EVICT:
                        evicted.add(s)
            elif isinstance(n, ast.Compare):
                capped |= _len_capped(n, match)
            if isinstance(n, (ast.If, ast.While)) and (on := _len_capped(n.test, match)):
                inner = ast.Module(body=n.body, type_ignores=[])
                _g, ev, _c, _gd = _ops([inner], match)
                guarded |= on & ev
    return grown, evicted, capped, guarded


def scan(source: str, rel: str) -> tuple[list[Site], list[str]]:
    """(sites, unbounded-memo findings) for one module's source."""
    tree = ast.parse(source)
    sites: list[Site] = []

    # 1. module-level containers grown inside a function
    module_names = {}
    for s in tree.body:
        target = (s.targets[0] if isinstance(s, ast.Assign) and len(s.targets) == 1
                  else s.target if isinstance(s, ast.AnnAssign) else None)
        if isinstance(target, ast.Name) and s.value is not None and _ctor(s.value):
            module_names[target.id] = s.lineno

    def by_name(e):
        return e.id if isinstance(e, ast.Name) and e.id in module_names else None

    grown, evicted, capped, guarded = _ops(_functions(tree), by_name)
    for name in sorted(grown):
        sites.append(Site(f"{rel}::{name}", module_names[name], name in evicted, name in capped,
                          name in guarded))

    # 2. process-lifetime instances of this module's classes, and class-body containers
    classes = {c.name: c for c in tree.body if isinstance(c, ast.ClassDef)}
    holders: dict[str, tuple[str, int]] = {}          # instance name -> (class, line)
    for s in tree.body:
        if isinstance(s, (ast.Assign, ast.AnnAssign)) and isinstance(s.value, ast.Call) \
                and isinstance(s.value.func, ast.Name) and s.value.func.id in classes:
            t = s.targets[0] if isinstance(s, ast.Assign) else s.target
            if isinstance(t, ast.Name):
                holders[t.id] = (s.value.func.id, s.lineno)
    for fn in _functions(tree):
        declared = {g for n in ast.walk(fn) if isinstance(n, ast.Global) for g in n.names}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) \
                    and isinstance(n.value.func, ast.Name) and n.value.func.id in classes:
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id in declared:
                        holders.setdefault(t.id, (n.value.func.id, n.lineno))

    for cname, cls in classes.items():
        attrs: dict[str, int] = {}
        for n in ast.walk(cls):
            if (isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None
                    and _ctor(n.value)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                            and t.value.id == "self":
                        attrs.setdefault(t.attr, n.lineno)
        body_attrs = {}
        for s in cls.body:
            if (isinstance(s, (ast.Assign, ast.AnnAssign)) and s.value is not None
                    and _ctor(s.value)):
                t = s.targets[0] if isinstance(s, ast.Assign) else s.target
                if isinstance(t, ast.Name):
                    body_attrs[t.id] = s.lineno

        def on_self(e, pool):
            return (e.attr if isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name)
                    and e.value.id in ("self", "cls", cname) and e.attr in pool else None)

        methods = _functions(cls)
        g, ev, cap, gd = _ops(methods, lambda e: on_self(e, attrs))
        for inst, (klass, _line) in sorted(holders.items()):
            if klass == cname:
                for a in sorted(g):
                    sites.append(Site(f"{rel}::{inst}.{a}", attrs[a], a in ev, a in cap,
                                      a in gd))
        g, ev, cap, gd = _ops(methods, lambda e: on_self(e, body_attrs))
        for a in sorted(g):
            sites.append(Site(f"{rel}::{cname}.{a}", body_attrs[a], a in ev, a in cap,
                              a in gd))

    # 3. unbounded memo decorators
    memo = []
    for fn in _functions(tree):
        for d in getattr(fn, "decorator_list", []):
            text = ast.unparse(d)
            bare = text.split("(")[0].rsplit(".", 1)[-1]
            if bare == "cache" or (bare == "lru_cache" and isinstance(d, ast.Call) and (
                    any(k.arg == "maxsize" and isinstance(k.value, ast.Constant)
                        and k.value.value is None for k in d.keywords)
                    or (d.args and isinstance(d.args[0], ast.Constant)
                        and d.args[0].value is None))):
                memo.append(f"{rel}:{fn.lineno} @{text} on {fn.name}: a memo that only grows")
    return sites, memo


def problems(sites: list[Site], ledger: dict) -> list[str]:
    out = []
    for s in sites:
        entry = ledger.get(s.key)
        if entry is None:
            out.append(f"{s.key} (line {s.line}): a process-lifetime container that code adds "
                       "to, with no stated bound. Bound it (LRU + TTL), or add it to LEDGER "
                       "with the kind of bound it has")
            continue
        kind, _why = entry
        if kind == "lru" and not (s.capped and s.guarded):
            gap = "no len() cap" if not s.capped else "no eviction inside its len() guard"
            out.append(f"{s.key}: ledgered as lru, but the code has {gap}")
        elif kind == "per-connection" and not s.evicts:
            out.append(f"{s.key}: ledgered as per-connection, but nothing removes an entry")
        elif kind not in ("lru", "per-connection", "config-keyed"):
            out.append(f"{s.key}: unknown bound kind {kind!r}")
    return out


def _scan_tree():
    sites, memo = [], []
    for path in SCANNED:
        rel = path.relative_to(ROOT).as_posix()
        got, m = scan(path.read_text(encoding="utf-8"), rel)
        sites += got
        memo += m
    return sites, memo


# ----------------------------------------------------------- the detector's controls

OOM = '''
_seen = {}

def handle(envelope):
    if envelope.id in _seen:
        return None
    _seen[envelope.id] = envelope          # the daemon-OOM shape: grows for ever
    return envelope
'''

LRU = '''
import collections
MAX = 100
_seen = collections.OrderedDict()

def handle(envelope):
    _seen[envelope.id] = envelope
    while len(_seen) > MAX:
        _seen.popitem(last=False)
'''


def test_the_detector_finds_the_daemon_oom_shape():
    sites, _ = scan(OOM, "planted.py")
    assert [s.key for s in sites] == ["planted.py::_seen"]
    assert problems(sites, {}) and "no stated bound" in problems(sites, {})[0]


def test_a_ledger_entry_cannot_call_an_unbounded_container_lru():
    sites, _ = scan(OOM, "planted.py")
    assert problems(sites, {"planted.py::_seen": ("lru", "claimed")}) == [
        "planted.py::_seen: ledgered as lru, but the code has no len() cap"]
    sites, _ = scan(LRU, "planted.py")                      # control: the real LRU passes
    assert problems(sites, {"planted.py::_seen": ("lru", "MAX entries")}) == []


#: The review's mutant MG2 (2026-09-26): a len() comparison that evicts nothing, beside a
#: reset that clears. Before the fix the reset satisfied "lru" and the cache grew for ever.
HOLLOW_LRU = '''
import collections
MAX = 100
_seen = collections.OrderedDict()

def handle(envelope):
    _seen[envelope.id] = envelope
    if len(_seen) > MAX:
        pass

def reset():
    _seen.clear()
'''

HOLLOW_LRU_INSTANCE = '''
import collections

class Cache:
    def __init__(self, size):
        self.size = size
        self._entries = collections.OrderedDict()
    def put(self, k, v):
        self._entries[k] = v
        while len(self._entries) > self.size:
            self._entries.get(k)
    def clear(self):
        self._entries.clear()

CACHE = Cache(10)
'''


@pytest.mark.parametrize("src,key", [(HOLLOW_LRU, "planted.py::_seen"),
                                     (HOLLOW_LRU_INSTANCE, "planted.py::CACHE._entries")],
                         ids=["module", "instance"])
def test_an_lru_needs_its_eviction_under_its_len_guard_not_anywhere(src, key):
    """A clear() in a reset, or any pop elsewhere, is not the eviction that keeps an LRU at its
    cap: the eviction must sit inside the if/while whose test compares len() of the cache."""
    sites, _ = scan(src, "planted.py")
    assert [s.key for s in sites] == [key]
    assert problems(sites, {key: ("lru", "claimed")}) == [
        f"{key}: ledgered as lru, but the code has no eviction inside its len() guard"]
    real = src.replace("        pass", "        _seen.popitem(last=False)").replace(
        "self._entries.get(k)", "self._entries.popitem(last=False)")
    assert real != src
    sites, _ = scan(real, "planted.py")                     # control: the evicting form passes
    assert problems(sites, {key: ("lru", "claimed")}) == []


def test_the_detector_sees_instances_class_bodies_and_globals():
    src = '''
class Holder:
    shared = {}
    def __init__(self):
        self.items = {}
    def put(self, k, v):
        self.items[k] = v
        Holder.shared.setdefault(k, v)

H = Holder()
_late = None

def boot():
    global _late
    _late = Holder()
'''
    keys = sorted(s.key for s in scan(src, "p.py")[0])
    assert keys == ["p.py::H.items", "p.py::Holder.shared", "p.py::_late.items"], keys


def test_a_table_built_at_import_is_not_a_cache():
    """Negative control of the detector: written only at module level, never in a function."""
    src = "TABLE = {}\nTABLE['a'] = 1\nTABLE.update(b=2)\n\ndef read(k):\n    return TABLE[k]\n"
    assert scan(src, "p.py")[0] == []


@pytest.mark.parametrize("decorator,found", [
    ("@functools.cache", True), ("@cache", True), ("@functools.lru_cache(maxsize=None)", True),
    ("@lru_cache(None)", True), ("@functools.lru_cache(maxsize=128)", False),
    ("@lru_cache", False)])
def test_an_unbounded_memo_decorator_is_found(decorator, found):
    src = (f"import functools\nfrom functools import cache, lru_cache\n"
           f"{decorator}\ndef f(x):\n    return x\n")
    assert bool(scan(src, "p.py")[1]) is found


# ----------------------------------------------------------------- the tree itself

def test_the_scan_covers_the_tree():
    """Assert the scan RAN over something: an empty glob would pass everything below."""
    assert len(SCANNED) > 100, len(SCANNED)
    assert any(p.name == "window.py" for p in SCANNED)


def test_every_process_lifetime_container_is_bounded():
    sites, memo = _scan_tree()
    assert sites, "the scan found no site at all: the detector is broken, not the tree clean"
    assert problems(sites, LEDGER) + memo == []


def test_the_ledger_names_only_sites_that_exist():
    found = {s.key for s in _scan_tree()[0]}
    stale = sorted(set(LEDGER) - found)
    assert stale == [], f"LEDGER entries the source no longer has: {stale}"
