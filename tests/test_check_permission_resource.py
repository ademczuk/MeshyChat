"""T2: every production call of `check_permission` says what it touches (`resource=`).

WHY (design `docs/design/biscuit-capabilities.md` 3.3, 3.4 and critique C5; divergence 040,
slice 2). A capability token authorises an OBJECT, a key or a key prefix, while the database
grants authorise a whole namespace. The token decision (slice 4) can only be as good as what
each call site tells it, so the signature gains a REQUIRED, keyword-only `resource`: a site
that does not say what it touches is a TypeError, never a silent namespace-wide default. In
this slice nothing reads it: the DB-grant decision is unchanged.

The table (design 3.4 at 0b6767f, eleven production sites; twelve and thirteen from the
review round, on main at 228a6ab with divergence 037's summaries; fourteen and fifteen from
divergence 053, redaction; sixteen and seventeen from divergence 059, edit; eighteen and
nineteen from POST /v1/mesh/hints):

   1  handlers.handle_grant              NAMESPACE_WIDE
   2  handlers.handle_write              Key(key)
   3  handlers.handle_read               Key(key)
   4  handlers.handle_query              Prefix(key_prefix or "")
   5  handlers.handle_list (namespace)   Prefix(prefix or "")
   6  handlers.handle_prune              Key(key), or NAMESPACE_WIDE when no key is given
   7  mesh.views.require_mesh_read       NAMESPACE_WIDE (from `_route` and `_mesh_read_refusal`)
   8  meshsem.views._route.wrapper       NAMESPACE_WIDE
   9  wsgw.mesh_notify.resolve_recipients Key(nudge.key)
  10  wsgw.revocation.has_mesh_read      NAMESPACE_WIDE
  11  meshsem.search._key_withheld         Key(the row's key), NEVER namespace-wide: the half
      of 027's body gate that answers whether memory.read would show THIS key, for the
      context anchor, the push preview and the addressed wait's preview (one key each)
  12  meshsum.views._route.<locals>.view   NAMESPACE_WIDE: the summary routes' gate, site 8's
      question (037's step 1)
  13  meshsem.search.namespace_body_withheld  NAMESPACE_WIDE: 027's gate asked about the
      namespace, for a route that shows a window of rows it has not read yet (037's steps 5
      and 10, re-asking site 12's question in the reading transaction)
  14  memoryskills.redact.handle_redact  Key(key): memory.redact's write permission on the row it withdraws
      (divergence 053; a redaction changes what every reader sees of ONE key)
  15  wsgw.mesh_notify.resolve_redaction_recipients  Key(red.nudge.key): the push of a redaction's event asks
      the gate a message frame asks, for the row's own key, never namespace-wide (divergence 053)
  16  memoryskills.edit.handle_edit  Key(key): memory.edit's write permission on the row it edits (divergence 059;
      an edit changes what every reader sees of ONE key)
  17  wsgw.mesh_notify.resolve_edit_recipients  Key(ed.nudge.key): the push of an edit's event asks the gate a
      message frame asks, for the row's own key, never namespace-wide (divergence 059)
  18  meshsem.send_hints._may_read  NAMESPACE_WIDE: pre-send hints' read gate on the card and context namespaces
  19  meshsem.views.hints  NAMESPACE_WIDE: POST /v1/mesh/hints's read gate on shared/mesh-chat, site 8's question

TWO INSTRUMENTS, because one could miss what the other sees:
  * static: an AST census of every production file that follows every binding of the
    function (the package re-export, `from .permissions import`, function-local imports,
    module aliases, `x = check_permission`, and, since the review round, any module that
    binds it: `handlers.check_permission`, `from apps.memoryskills.handlers import
    check_permission as cp`), and flags the name used as a value or as a string. It follows
    027's body gate the same way. It must find exactly the table's sites; its positive
    controls are that it finds the table's nineteen on the tree it runs on, that it finds
    each import form in a planted source, and that every binding the RUNNING code has
    (found by identity over every loaded production module) is one it follows;
  * dynamic: each site DRIVEN with a fixture while a spy (patched into every module that
    binds the real function) records its arguments. Each drive asserts the exact resource,
    in type and in key or prefix, and that no call came from anywhere else.

RED on the unchanged tree: no site passes `resource`, and `Key`, `Prefix`, `NAMESPACE_WIDE`
do not exist. Imports of them are inside the tests, so each test is red for its own reason.

Run:  pytest tests/test_check_permission_resource.py -q -p no:cacheprovider
"""
from __future__ import annotations

import ast
import collections
import dataclasses
import datetime as _dt
import inspect
import json
import os
import sys
import uuid
from pathlib import Path

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("MESHY_TEST_DSN",
                     "postgresql://meshychat_owner:change-me@127.0.0.1:5434/meshy_django")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "meshychat.settings")
os.environ.setdefault("MESHY_ALLOWED_HOSTS", "testserver,127.0.0.1,localhost")

ISSUER = "meshy-t2-resource-issuer"
_signing_key = ed25519.Ed25519PrivateKey.generate()
JWT_PRIVATE_PEM = _signing_key.private_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption())
JWT_PUBLIC_PEM = _signing_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo)

import django  # noqa: E402
from django.conf import settings as dj_settings  # noqa: E402

if not dj_settings.configured:  # pragma: no cover
    django.setup()

import jwt as pyjwt  # noqa: E402
from django.test import Client, override_settings  # noqa: E402

PERMISSIONS = "apps.memoryskills.permissions"
PACKAGE = "apps.memoryskills"
FUNC = "check_permission"

#: site -> (file, enclosing function's __qualname__, the `resource=` expression as
#: `ast.unparse` prints it). The expression is part of the table on purpose: which
#: resource a site passes is a security decision, and changing it must change this line.
SITES = {
    1: ("apps/memoryskills/handlers.py", "handle_grant", "NAMESPACE_WIDE"),
    2: ("apps/memoryskills/handlers.py", "handle_write", "Key(key)"),
    3: ("apps/memoryskills/handlers.py", "handle_read", "Key(key)"),
    4: ("apps/memoryskills/handlers.py", "handle_query", "Prefix(key_prefix or '')"),
    5: ("apps/memoryskills/handlers.py", "handle_list", "Prefix(prefix or '')"),
    6: ("apps/memoryskills/handlers.py", "handle_prune",
        "Key(key) if key is not None else NAMESPACE_WIDE"),
    7: ("apps/mesh/views.py", "require_mesh_read", "NAMESPACE_WIDE"),
    8: ("apps/meshsem/views.py", "_route.<locals>.wrapper", "NAMESPACE_WIDE"),
    9: ("apps/wsgw/mesh_notify.py", "resolve_recipients", "Key(nudge.key)"),
    10: ("apps/wsgw/revocation.py", "has_mesh_read", "NAMESPACE_WIDE"),
    11: ("apps/meshsem/search.py", "_key_withheld", "Key(key)"),
    12: ("apps/meshsum/views.py", "_route.<locals>.view", "NAMESPACE_WIDE"),
    13: ("apps/meshsem/search.py", "namespace_body_withheld", "NAMESPACE_WIDE"),
    14: ("apps/memoryskills/redact.py", "handle_redact", "Key(key)"),
    15: ("apps/wsgw/mesh_notify.py", "resolve_redaction_recipients", "Key(red.nudge.key)"),
    16: ("apps/memoryskills/edit.py", "handle_edit", "Key(key)"),
    17: ("apps/wsgw/mesh_notify.py", "resolve_edit_recipients", "Key(ed.nudge.key)"),
    18: ("apps/meshsem/send_hints.py", "_may_read", "NAMESPACE_WIDE"),
    19: ("apps/meshsem/views.py", "hints", "NAMESPACE_WIDE"),
}
SITE_OF = {(path, qual): n for n, (path, qual, _expr) in SITES.items()}

SEARCH, NOTIFY = "apps.meshsem.search", "apps.wsgw.mesh_notify"
CP_ID = f"{PERMISSIONS}.{FUNC}"

#: 027's body gate, as every function production reaches it by: (defining module, name).
#: The census follows each through every binding, as it follows check_permission.
GATES = frozenset({
    (SEARCH, "_reader_withheld"), (SEARCH, "_key_withheld"), (SEARCH, "_anchor_body_withheld"),
    (SEARCH, "body_withheld_many"), (SEARCH, "namespace_body_withheld"),
    (NOTIFY, "body_withheld"), (NOTIFY, "body_withheld_many"), (NOTIFY, "preview_gate"),
    (NOTIFY, "preview_gates"),
})
FOLLOWED = frozenset({(PERMISSIONS, FUNC)}) | GATES

#: Every production call of the gate: (file, enclosing function, the gate it reaches, by
#: its defining module and name) -> the arguments after (conn, reader), one tuple per call.
#: The row's key, or the page's keys; a window's gate takes none (it asks about the
#: namespace), and nothing else calls it.
GATE_CALLERS = {
    ("apps/meshsem/search.py", "_anchor_body_withheld", f"{SEARCH}._reader_withheld"): [()],
    ("apps/meshsem/search.py", "_anchor_body_withheld", f"{SEARCH}._key_withheld"):
        [("key",)],
    ("apps/meshsem/search.py", "body_withheld_many", f"{SEARCH}._reader_withheld"): [()],
    ("apps/meshsem/search.py", "body_withheld_many", f"{SEARCH}._key_withheld"): [("key",)],
    ("apps/meshsem/search.py", "namespace_body_withheld", f"{SEARCH}._reader_withheld"): [()],
    ("apps/meshsem/search.py", "context_for", f"{SEARCH}._anchor_body_withheld"):
        [("anchor['key']",)],
    ("apps/wsgw/mesh_notify.py", "body_withheld", f"{SEARCH}._anchor_body_withheld"):
        [("key",)],
    ("apps/wsgw/mesh_notify.py", "body_withheld_many", f"{SEARCH}.body_withheld_many"):
        [("keys",)],
    ("apps/wsgw/mesh_notify.py", "preview_gate", f"{NOTIFY}.body_withheld"): [("key",)],
    ("apps/wsgw/mesh_notify.py", "preview_gates", f"{NOTIFY}.body_withheld_many"):
        [("keys",)],
    ("apps/wsgw/mesh_notify.py", "push_frame", f"{NOTIFY}.preview_gate"): [("nudge.key",)],
    ("apps/mesh/wait.py", "scan_addressed", f"{NOTIFY}.preview_gates"): [("gated",)],
    # #566: the thread page's ?envelope=1 gates exactly the page's own message keys (deduplicated) through the
    # same memory.read gate as previews; a withheld key gets no envelope, only the reason.
    ("apps/mesh/views.py", "_envelopes", f"{NOTIFY}.preview_gates"): [("keys",)],
    ("apps/meshsum/views.py", "_admitted", f"{SEARCH}.namespace_body_withheld"): [(), ()],
    ("apps/meshsum/views.py", "_summarize_deterministic", f"{SEARCH}.namespace_body_withheld"): [()],
}


# =========================================================================== the census

def _production_files(root: Path) -> list[Path]:
    out = [root / "manage.py"] if (root / "manage.py").exists() else []
    for top in ("apps", "meshychat", "tools"):
        out.extend(p for p in sorted((root / top).rglob("*.py")) if "__pycache__" not in p.parts)
    return out


def _module_name(rel: str) -> tuple[str, bool]:
    parts = rel[:-3].split("/")
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts = parts[:-1]
    return ".".join(parts), is_pkg


def _absolute(module: str, is_pkg: bool, level: int, name: str | None) -> str:
    if level == 0:
        return name or ""
    base = module.split(".") if is_pkg else module.split(".")[:-1]
    if level > 1:
        base = base[:len(base) - (level - 1)]
    return ".".join(base + ([name] if name else []))


@dataclasses.dataclass
class Found:
    path: str
    qualname: str
    line: int
    kind: str                  # CALL, VALUE (the function used as a value), STRING, STAR
    positional: int = 0
    resource: str | None = None
    target: str = CP_ID        # the followed function reached: "<defining module>.<name>"
    rest: tuple = ()           # the positional arguments after the first two, unparsed


@dataclasses.dataclass(frozen=True)
class Module:
    name: str
    rel: str
    is_pkg: bool
    tree: ast.Module = dataclasses.field(compare=False)


def _modules(root: Path) -> dict[str, Module]:
    out = {}
    for path in _production_files(root):
        rel = path.relative_to(root).as_posix()
        name, is_pkg = _module_name(rel)
        out[name] = Module(name, rel, is_pkg, ast.parse(path.read_text(encoding="utf-8"), rel))
    return out


def _known(mods: dict) -> frozenset[str]:
    """Every module and package name the production files make importable."""
    known = set()
    for name in mods:
        parts = name.split(".")
        known.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
    return frozenset(known)


def _import_time(tree: ast.Module):
    """The statements that run when the module is imported: its body and the blocks of its
    if/try/with/for/while, never a function or class body. What they bind is what another
    module can import from it."""
    todo = list(tree.body)
    while todo:
        node = todo.pop(0)
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list):
                todo.extend(block)
        for handler in getattr(node, "handlers", None) or []:
            todo.extend(handler.body)


def _resolve(node, names: dict, exports: dict, known: frozenset):
    """What an expression refers to: ("fn", target) for a followed function, ("mod", name)
    for a module, else None. `a.b` is a submodule when one exists, else what module `a`
    binds as `b` at import time."""
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        base = _resolve(node.value, names, exports, known)
        if base is not None and base[0] == "mod":
            sub = f"{base[1]}.{node.attr}"
            if sub in known:
                return ("mod", sub)
            return exports.get(base[1], {}).get(node.attr)
    return None


def _bindings(mod: Module, statements, exports: dict, known: frozenset) -> dict:
    """The names `statements` bind in `mod` to a followed function or to a module. Every
    import form: `import a.b` (binds `a`), `import a.b as x`, `from m import n as x` where
    `m.n` is a submodule or something module `m` binds (so a module that imported the
    function, a re-export, is a way to it), and `x = <any of those>` chains. A module's own
    `def` of a followed function binds it too."""
    names = dict(exports.get(mod.name, {}))
    for m, fname in FOLLOWED:
        if m == mod.name and any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                 and n.name == fname for n in mod.tree.body):
            names[fname] = ("fn", f"{m}.{fname}")
    statements = list(statements)
    for node in statements:
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.asname:
                    names[a.asname] = ("mod", a.name)
                else:
                    top = a.name.split(".")[0]
                    names[top] = ("mod", top)
        elif isinstance(node, ast.ImportFrom):
            src = _absolute(mod.name, mod.is_pkg, node.level, node.module)
            for a in node.names:
                if a.name == "*":
                    continue
                bound = a.asname or a.name
                if f"{src}.{a.name}" in known:
                    names[bound] = ("mod", f"{src}.{a.name}")
                elif a.name in exports.get(src, {}):
                    names[bound] = exports[src][a.name]
    changed = True
    while changed:                               # x = check_permission, y = x, ...
        changed = False
        for node in statements:
            targets, value = [], None
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
                targets, value = [node.target], node.value
            b = _resolve(value, names, exports, known) if value is not None else None
            for t in targets:
                if b and isinstance(t, ast.Name) and names.get(t.id) != b:
                    names[t.id] = b
                    changed = True
    return names


def exports_of(mods: dict) -> dict[str, dict]:
    """What each production module binds at import time, to a followed function or to a
    module, to a fixed point ACROSS modules: a module that imports the function from a
    module that imported it re-exports it too. This is the review round's answer to F4: the
    census before it knew two exporters (the permissions module and the package)."""
    known = _known(mods)
    exports: dict = {name: {} for name in mods}
    for _ in range(64):
        new = {name: _bindings(m, _import_time(m.tree), exports, known)
               for name, m in mods.items()}
        if new == exports:
            return exports
        exports = new
    raise AssertionError("the export table did not reach a fixed point")


_WORLDS: dict = {}


def _world(root: Path = ROOT):
    key = str(root)
    if key not in _WORLDS:
        mods = _modules(root)
        _WORLDS[key] = (exports_of(mods), _known(mods))
    return _WORLDS[key]


def census_source(source: str, rel: str, root: Path = ROOT) -> list[Found]:
    """Every use of `check_permission` and of 027's gate (FOLLOWED) in one file, following
    every binding of each through the production modules' export table. Names are tracked
    file-wide (not per scope): an over-approximation, which can only make the census report
    MORE than the truth, never less."""
    exports, known = _world(root)
    tree = ast.parse(source, rel)
    module, is_pkg = _module_name(rel)
    mod = Module(module, rel, is_pkg, tree)
    names = _bindings(mod, ast.walk(tree), exports, known)
    followed_names = {fname for _m, fname in FOLLOWED}
    found: list[Found] = []

    def target(node) -> str | None:
        b = _resolve(node, names, exports, known)
        return b[1] if b is not None and b[0] == "fn" else None

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            src = _absolute(module, is_pkg, node.level, node.module)
            if any(b[0] == "fn" for b in exports.get(src, {}).values()):
                found.append(Found(rel, "<module>", node.lineno, "STAR"))

    class Walk(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[str] = []
            self.callees: set[int] = set()
            self.aliases: set[int] = set()

        def _qual(self) -> str:
            return ".".join(self.stack) if self.stack else "<module>"

        def _scope(self, node, name, is_function):
            self.stack.append(name)
            if is_function:
                self.stack.append("<locals>")
            self.generic_visit(node)
            if is_function:
                self.stack.pop()
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self._scope(node, node.name, True)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            self._scope(node, "<lambda>", True)

        def visit_ClassDef(self, node):
            self._scope(node, node.name, False)

        def _here(self) -> str:
            q = self._qual()
            return q[:-len(".<locals>")] if q.endswith(".<locals>") else q

        def visit_Assign(self, node):
            if target(node.value):
                self.aliases.add(id(node.value))
            self.generic_visit(node)

        def visit_Call(self, node):
            t = target(node.func)
            if t is not None:
                self.callees.add(id(node.func))
                res = next((k for k in node.keywords if k.arg == "resource"), None)
                found.append(Found(rel, self._here(), node.lineno, "CALL", len(node.args),
                                   ast.unparse(res.value) if res else None, t,
                                   tuple(ast.unparse(a) for a in node.args[2:])))
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if isinstance(arg, ast.Constant) and arg.value in followed_names:
                    found.append(Found(rel, self._here(), node.lineno, "STRING",
                                       target=str(arg.value)))
            self.generic_visit(node)

        def _value_use(self, node):
            t = target(node)
            if t is not None and id(node) not in self.callees and id(node) not in self.aliases \
                    and not isinstance(getattr(node, "ctx", None), ast.Store):
                found.append(Found(rel, self._here(), node.lineno, "VALUE", target=t))

        def visit_Name(self, node):
            self._value_use(node)

        def visit_Attribute(self, node):
            self._value_use(node)
            self.generic_visit(node)

    Walk().visit(tree)
    return found


def census(root: Path = ROOT) -> list[Found]:
    out: list[Found] = []
    for path in _production_files(root):
        rel = path.relative_to(root).as_posix()
        out.extend(census_source(path.read_text(encoding="utf-8"), rel, root))
    return out


def _cp(found: list) -> list:
    """The census's findings about check_permission (it follows 027's gate as well)."""
    return [f for f in found if getattr(f, "target", CP_ID) == CP_ID]


def test_the_census_finds_exactly_the_tables_nineteen_sites():
    """The positive control the rest of the file stands on: on the tree it runs on, the
    census finds nineteen CALL sites, they are the table's (file, enclosing function), and
    neither the function nor 027's gate is ever used as a value, a string or a star
    import."""
    got = census()
    calls = [f for f in _cp(got) if f.kind == "CALL"]
    other = [f for f in got if f.kind != "CALL"]
    assert len(calls) == 19, [(f.path, f.qualname, f.line) for f in calls]
    assert {(f.path, f.qualname) for f in calls} == set(SITE_OF), \
        sorted({(f.path, f.qualname) for f in calls} ^ set(SITE_OF))
    assert not other, [(f.kind, f.path, f.qualname, f.line) for f in other]


PLANTED = '''
import apps.memoryskills.permissions as P1
import apps.memoryskills
from apps import memoryskills as ms
from apps.memoryskills import permissions as perms, check_permission as reexported
from apps.memoryskills.permissions import check_permission as renamed
from .permissions import check_permission

def direct(): check_permission("n", 1, "read", None)
def via_package(): reexported("n", 1, "read", None)
def via_rename(): renamed("n", 1, "read", None, resource=NAMESPACE_WIDE)
def via_module_alias(): P1.check_permission("n", 1, "read", None)
def via_package_attr(): ms.permissions.check_permission("n", 1, "read", None)
def via_dotted(): apps.memoryskills.permissions.check_permission("n", 1, "read", None)
def via_perms(): perms.check_permission("n", 1, "read", None)
def via_assignment():
    cp = renamed
    again = cp
    again("n", 1, "read", None)
def local_import():
    from apps.memoryskills.permissions import check_permission as late
    late("n", 1, "read", None)
def as_value(): run(check_permission)
def as_string(): getattr(perms, "check_permission")
class C:
    def method(self):
        def inner(): check_permission("n", 1, "read", None)
        return inner
'''


PLANTED_VIA_BINDERS = '''
from apps.memoryskills import handlers
from apps.memoryskills.handlers import check_permission as cp
import apps.mesh.views as mesh_views
from apps.meshsem.search import body_withheld as bw
from apps.meshsem import search as S
from apps.wsgw.mesh_notify import preview_gate as pg

def via_handlers_attr(): handlers.check_permission("n", 1, "read", None, resource=NAMESPACE_WIDE)
def via_handlers_name(): cp("n", 1, "read", None)
def via_views_attr(): mesh_views.check_permission("n", 1, "read", None)
def gate_alias(): bw(conn, reader, "msg/x")
def gate_module(): S.body_withheld(conn, reader, key)
def gate_notify_alias(): pg(conn, reader, key)
'''


def test_the_census_follows_a_binding_made_through_another_module():
    """F4 of the slice-2 review: a module that BINDS the function (handlers, mesh.views,
    every site module) is a way to reach it, by attribute or by `from ... import ... as`,
    and so is an alias of 027's gate. The census before the review round matched only the
    permissions module and the package, and the gate census matched callee NAMES, so each
    of these was invisible to it (the reviewer's X2, X2b, X3). Each is found, in its
    enclosing function, reaching the right function."""
    got = census_source(PLANTED_VIA_BINDERS, "apps/memoryskills/planted_binders.py")
    calls = {f.qualname: f for f in got if f.kind == "CALL"}
    assert set(calls) == {"via_handlers_attr", "via_handlers_name", "via_views_attr",
                          "gate_alias", "gate_module", "gate_notify_alias"}, sorted(calls)
    assert {q: getattr(calls[q], "target", None) for q in calls} == {
        "via_handlers_attr": CP_ID, "via_handlers_name": CP_ID, "via_views_attr": CP_ID,
        "gate_alias": f"{SEARCH}._anchor_body_withheld",
        "gate_module": f"{SEARCH}._anchor_body_withheld",
        "gate_notify_alias": f"{NOTIFY}.preview_gate"}
    assert calls["via_handlers_attr"].resource == "NAMESPACE_WIDE"
    assert calls["via_handlers_name"].resource is None
    assert getattr(calls["gate_alias"], "rest", None) == ("'msg/x'",)


def test_the_census_follows_every_binding_the_running_code_has():
    """The census's link to the running code (F4 of the slice-2 review): every production
    module under apps/ is imported, and every attribute of every loaded production module
    that IS check_permission or one of 027's gate functions (by identity, as the spy finds
    them) is reached twice in a planted source, as `from M import name` and as `M.name`.
    The census must find each call and name the function it reaches. A binding form the
    census does not follow is a binding the running code has and this test plants.
    Control: the known bindings are among those found (the package's re-export, handlers,
    the views, mesh_notify's, search's alias `body_withheld`).

    A `__main__` module is not imported: it is an entry point the server never imports, and
    importing one runs it. main's `apps/memoryskills/__main__.py` (039's CLI, integ/r8)
    ends in an unguarded `raise SystemExit(main())`, which made this test exit 2 on a trial
    merge of this branch (divergence 040, slice 3). The static census still reads those
    files."""
    import importlib

    mods = sorted(_module_name(p.relative_to(ROOT).as_posix())[0]
                  for p in _production_files(ROOT))
    for name in mods:
        if name.startswith("apps.") and not name.endswith(".__main__"):
            importlib.import_module(name)
    real = {}
    for m, fname in FOLLOWED:
        fn = getattr(sys.modules[m], fname, None)
        if fn is not None:
            real[id(fn)] = (fn, f"{m}.{fname}")
    bindings = []
    for name in mods:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for attr, value in vars(mod).items():
            hit = real.get(id(value))
            if hit is not None and hit[0] is value:
                bindings.append((name, attr, hit[1]))
    assert {("apps.memoryskills", FUNC, CP_ID), ("apps.memoryskills.handlers", FUNC, CP_ID),
            ("apps.mesh.views", FUNC, CP_ID), ("apps.meshsem.views", FUNC, CP_ID),
            ("apps.meshsem.send_hints", FUNC, CP_ID),
            ("apps.wsgw.mesh_notify", FUNC, CP_ID),
            (SEARCH, "body_withheld", f"{SEARCH}._anchor_body_withheld"),
            (NOTIFY, "preview_gate", f"{NOTIFY}.preview_gate")} <= set(bindings), bindings
    missed = []
    for n, (name, attr, target) in enumerate(sorted(bindings)):
        source = (f"import {name} as m{n}\n"
                  f"from {name} import {attr} as f{n}\n"
                  f"def by_attr(): m{n}.{attr}(conn, reader, key)\n"
                  f"def by_name(): f{n}(conn, reader, key)\n")
        got = {(f.qualname, getattr(f, "target", CP_ID)) for f in
               census_source(source, "apps/memoryskills/planted_runtime.py")
               if f.kind == "CALL"}
        for fn in ("by_attr", "by_name"):
            if (fn, target) not in got:
                missed.append((name, attr, target, fn))
    assert len(bindings) >= 7 and not missed, missed


def test_the_census_follows_every_import_form_it_claims_to():
    """The census's own positive control: a planted module (never written to disk) that
    reaches the function through every binding the design names, and uses it as a value
    and as a string. Each is found, in the right enclosing function, with its resource."""
    got = census_source(PLANTED, "apps/memoryskills/planted.py")
    calls = {f.qualname: f for f in got if f.kind == "CALL"}
    assert set(calls) == {"direct", "via_package", "via_rename", "via_module_alias",
                          "via_package_attr", "via_dotted", "via_perms", "via_assignment",
                          "local_import", "C.method.<locals>.inner"}, sorted(calls)
    assert calls["via_rename"].resource == "NAMESPACE_WIDE"
    assert calls["direct"].resource is None and calls["direct"].positional == 4
    assert {(f.kind, f.qualname) for f in got if f.kind != "CALL"} == {
        ("VALUE", "as_value"), ("STRING", "as_string")}
    # Negative control: a same-named function from elsewhere is not the function.
    other = census_source("from somewhere.other import check_permission\n"
                          "def f(): check_permission(1, 2, 3, 4)\n", "apps/x/other.py")
    assert other == []


def test_every_site_passes_the_tables_resource_statically():
    """Each of the nineteen passes `resource=` as a keyword, with the table's expression, and
    exactly the four positional arguments the decision reads. RED before the change: no
    site passes `resource` at all."""
    calls = [f for f in _cp(census()) if f.kind == "CALL"]
    wrong = {}
    for f in calls:
        n = SITE_OF.get((f.path, f.qualname))
        expected = SITES[n][2] if n else "<not in the table>"
        if f.resource != expected or f.positional != 4:
            wrong[n or (f.path, f.qualname)] = (f.resource, f.positional, expected)
    assert len(calls) == 19 and not wrong, wrong


def test_every_caller_of_027s_gate_passes_the_rows_key_or_the_pages_keys():
    """027's gate answers for ONE key (site 11) or, for a window it has not read, for the
    namespace (site 13). Every production call of it, through any binding, is the table's:
    the context anchor, the push and 033's wrappers pass the row's key, the addressed wait
    passes the page's gated keys in one call, the summaries' two steps ask about the
    namespace, and the gate's own halves are called only from the gate. Nothing passes a key
    the table does not name, and nothing else calls it."""
    seen: dict = collections.defaultdict(list)
    for f in census():
        target = getattr(f, "target", CP_ID)
        if f.kind == "CALL" and target != CP_ID:
            seen[(f.path, f.qualname, target)].append(getattr(f, "rest", None))
    got = {k: sorted(v) for k, v in seen.items()}
    want = {k: sorted(v) for k, v in GATE_CALLERS.items()}
    assert got == want, {"unexpected": {k: v for k, v in got.items() if want.get(k) != v},
                         "missing": {k: v for k, v in want.items() if got.get(k) != v}}


# ======================================================================= the signature

class _Reached(Exception):
    """Raised by the sentinel connection: the check got past its argument checks."""


class _Sentinel:
    def __getattr__(self, name):
        raise _Reached(name)


def test_resource_is_required_keyword_only_and_its_type_is_checked():
    """No default, keyword-only: a call that does not say what it touches is a TypeError,
    and so is a positional fifth argument or a value of any other type, before the database
    is touched (the sentinel connection raises on any use). Control: each of the three
    real kinds gets past the check and reaches the database."""
    from apps.memoryskills.permissions import NAMESPACE_WIDE, Key, Prefix, check_permission

    param = inspect.signature(check_permission).parameters["resource"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty

    with pytest.raises(TypeError, match="resource"):
        check_permission("ns/x", uuid.uuid4(), "read", _Sentinel())
    with pytest.raises(TypeError):
        check_permission("ns/x", uuid.uuid4(), "read", _Sentinel(), NAMESPACE_WIDE)

    class KeyLike(Key):
        pass

    for bad in ("k", None, ("key", "k"), {"k": "k"}, Key, KeyLike("k"), object()):
        with pytest.raises(TypeError, match="resource"):
            check_permission("ns/x", uuid.uuid4(), "read", _Sentinel(), resource=bad)

    for good in (Key("k"), Key(""), Prefix("p/"), Prefix(""), NAMESPACE_WIDE):
        with pytest.raises(_Reached):
            check_permission("ns/x", uuid.uuid4(), "read", _Sentinel(), resource=good)


def test_the_three_kinds_compare_by_kind_and_value_and_are_re_exported():
    """Key and Prefix are values: equal when kind and string are, never equal across
    kinds; frozen; NAMESPACE_WIDE is the one instance of its type. All three are exported
    by the package beside check_permission (apps/memoryskills/__init__.py)."""
    import apps.memoryskills as pkg
    from apps.memoryskills import permissions as perm

    Key, Prefix, WIDE = perm.Key, perm.Prefix, perm.NAMESPACE_WIDE
    assert Key("a") == Key("a") and hash(Key("a")) == hash(Key("a"))
    assert Prefix("a") == Prefix("a")
    assert Key("a") != Key("b") and Prefix("a") != Prefix("b")
    assert Key("a") != Prefix("a") and Prefix("a") != Key("a")
    assert Key("a") != WIDE and Prefix("") != WIDE
    with pytest.raises(dataclasses.FrozenInstanceError):
        Key("a").k = "b"
    with pytest.raises(dataclasses.FrozenInstanceError):
        Prefix("a").p = "b"
    assert type(WIDE)() is WIDE
    assert repr(WIDE) == "NAMESPACE_WIDE"
    for name in ("Key", "Prefix", "NAMESPACE_WIDE", "check_permission"):
        assert getattr(pkg, name) is getattr(perm, name), name
        assert name in pkg.__all__, name


def test_the_decision_does_not_read_the_resource(owner):
    """Behaviour identical (this slice builds no decision): for the owner, a read grant,
    a revoked grant and a stranger, every resource gives the answer the namespace alone
    gives, the same dict or the same typed refusal. A decision that read the resource
    would split one of these rows."""
    from apps.core import rls
    from apps.memoryskills.errors import HandlerError
    from apps.memoryskills.permissions import NAMESPACE_WIDE, Key, Prefix, check_permission

    claimant = _peer(owner, "decide-owner")
    ns = _claim(owner, claimant)
    reader, revoked, stranger = (_peer(owner, t) for t in ("decide-read", "decide-rev",
                                                            "decide-none"))
    _grant(owner, ns, reader, claimant, "read")
    _grant(owner, ns, revoked, claimant, "write", revoked=True)
    resources = [NAMESPACE_WIDE, Key("a/1"), Key(""), Key("zzz"), Prefix(""), Prefix("a/"),
                 Prefix("nowhere/")]
    answers = {}
    with rls.server_internal(owner):
        for tag, p in (("owner", claimant), ("read", reader), ("revoked", revoked),
                       ("stranger", stranger)):
            for need in ("read", "write", "admin"):
                seen = set()
                for r in resources:
                    try:
                        seen.add(json.dumps(check_permission(ns, p["peer_id"], need, owner,
                                                             resource=r), sort_keys=True))
                    except HandlerError as exc:
                        seen.add(exc.code)
                assert len(seen) == 1, (tag, need, seen)
                answers[(tag, need)] = seen.pop()
    assert json.loads(answers[("owner", "admin")])["via"] == "owner"          # controls
    assert json.loads(answers[("read", "read")])["via"] == "grant"
    assert answers[("read", "write")] == "E_PERMISSION_DENIED"
    assert answers[("revoked", "read")] == answers[("stranger", "read")] == "E_PERMISSION_DENIED"


# ===================================================================== the driven sites

@pytest.fixture(autouse=True)
def t2_settings():
    with override_settings(ROOT_URLCONF="meshychat.urls", ALLOWED_HOSTS=["*"],
                           MESHY_JWT_PUBLIC_KEY_PEM=JWT_PUBLIC_PEM, MESHY_JWT_ISSUER=ISSUER,
                           MESHY_DSN=DSN, MESHY_DPOP_ENFORCE=False):
        yield


@pytest.fixture(autouse=True)
def no_roster():
    from apps.memoryskills import handlers
    handlers.prime_roster_labels(b"{}")
    yield
    handlers.prime_roster_labels(b"{}")


@pytest.fixture()
def owner():
    with psycopg.connect(DSN, autocommit=True) as c:
        yield c


@pytest.fixture()
def app_conn():
    from apps.memoryskills import db
    conn = db.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def memory_authority(owner):
    from tests.fixtures import meshsem_db as db
    mem = db.memory_authority(owner)
    with override_settings(MESHY_MEMORY_PEER_ID=str(mem["peer_id"])):
        yield mem


def _peer(conn, tag: str, *, group=None, key: bool = True) -> dict:
    from tests.fixtures import meshsem_db as db
    owner_id, peer_id, group = uuid.uuid4(), uuid.uuid4(), group or uuid.uuid4()
    name = f"t2-{tag}-{peer_id.hex[:8]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO dashboard_users (id, email, plane, trust_group_id)"
                    " VALUES (%s,%s,'dev',%s)",
                    (owner_id, f"t2-{tag}-{owner_id.hex[:8]}@example.test", group))
        cur.execute("INSERT INTO peers (id, owner_id, name, display_name) VALUES (%s,%s,%s,%s)",
                    (peer_id, owner_id, name, f"t2 {tag}"))
    p = {"peer_id": peer_id, "owner_id": owner_id, "name": name, "label": name,
         "group": group}
    if key:
        p.update(db.add_key(conn, peer_id))
    return p


def _claim(conn, p: dict, ns: str | None = None) -> str:
    ns = ns or f"t2test/{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_namespace (namespace, owner_peer_id,"
                    " owner_owner_id, claimant_plane, ephemeral, retention_policy)"
                    " VALUES (%s,%s,%s,'dev',FALSE,'keep_all') ON CONFLICT DO NOTHING",
                    (ns, p["peer_id"], p["owner_id"]))
    return ns


def _mesh_ns(conn) -> str:
    from tests.fixtures import meshsem_db as db
    db.ensure_namespace(conn)
    return db.NS


def _grant(conn, ns: str, grantee: dict, granter: dict, permission: str = "read",
           *, revoked: bool = False) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO shared_memory_grant (namespace, grantee_peer_id,"
                    " grantee_owner_id, permission, granted_by_peer_id, granted_by_owner_id,"
                    " revoked) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (ns, grantee["peer_id"], grantee["owner_id"], permission,
                     granter["peer_id"], granter["owner_id"], revoked))


def _mesh_grant(conn, p: dict, permission: str = "read") -> None:
    from tests.fixtures import meshsem_db as db
    holder = db.ensure_namespace(conn)
    _grant(conn, db.NS, p, {"peer_id": holder[0], "owner_id": holder[1]}, permission)


def _mesh_row(conn, author: dict, value: dict) -> str:
    from tests.fixtures import meshsem_db as db
    return db.row(conn, author, value)["key"]


def _envelope(peer: dict, skill: str, args: dict) -> dict:
    from apps.crypto import primitives as Pr
    now = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    payload = {"v": 1, "kind": "skill.call", "call_id": f"call-{uuid.uuid4()}",
               "caller_peer_id": str(peer["peer_id"]), "target_peer_id": str(peer["peer_id"]),
               "skill_name": skill, "skill_version": "1", "args": args,
               "args_attachments": [], "issued_at": now}
    return {"payload": payload, "signature": Pr.b64u(Pr.sign(peer["seed"], Pr.jcs(payload))),
            "signing_kid": peer["kid"], "verified_at": now}


def _dispatch(conn, peer: dict, skill: str, args: dict):
    from apps.memoryskills import handlers
    from apps.memoryskills.errors import HandlerError
    try:
        return handlers.dispatch(skill, _envelope(peer, skill, args), peer["peer_id"], conn)
    except HandlerError as exc:
        return exc


def _auth(peer_id) -> dict:
    now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    token = pyjwt.encode({"sub": str(peer_id), "iss": ISSUER, "iat": now, "exp": now + 300,
                          "aud": "peer", "scopes": []}, JWT_PRIVATE_PEM, algorithm="EdDSA")
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@dataclasses.dataclass
class Seen:
    site: int | None           # None: a caller the table does not have
    where: tuple[str, str]
    via: tuple[str, ...]       # the next frames up, by qualname
    resource: tuple


def _describe(kwargs: dict) -> tuple:
    """The resource as ("key", k) / ("prefix", p) / ("namespace_wide",), by EXACT type;
    ("absent",) when the keyword was not passed (the tree before this slice)."""
    if "resource" not in kwargs:
        return ("absent",)
    r = kwargs["resource"]
    from apps.memoryskills import permissions as perm
    if type(r) is perm.Key:
        return ("key", r.k)
    if type(r) is perm.Prefix:
        return ("prefix", r.p)
    if r is perm.NAMESPACE_WIDE:
        return ("namespace_wide",)
    return ("unknown", repr(r))


@pytest.fixture()
def spy(monkeypatch):
    """Every call of the real check_permission from production code, as `Seen`. Patched
    into EVERY loaded module that binds the real function (a module-level `from ... import`),
    and into the permissions module, which is what a function-local import reads at call
    time. It calls through, so every decision is the real one."""
    import importlib

    for name in ("apps.memoryskills", "apps.memoryskills.handlers", "apps.memoryskills.redact",
                 "apps.memoryskills.edit", "apps.mesh.views", "apps.meshsem.views", "apps.meshsem.send_hints",
                 "apps.meshsem.search", "apps.wsgw.mesh_notify",
                 "apps.wsgw.revocation", "apps.mesh.wait", "apps.meshsum.views"):
        importlib.import_module(name)
    from apps.memoryskills import permissions as perm
    real = perm.check_permission
    seen: list[Seen] = []

    def _resource_spy(*args, **kwargs):
        caller = sys._getframe(1)
        try:
            rel = Path(caller.f_code.co_filename).resolve().relative_to(ROOT).as_posix()
        except ValueError:
            rel = caller.f_code.co_filename
        if not rel.startswith("tests/"):
            where = (rel, caller.f_code.co_qualname)
            via, f = [], caller.f_back
            while f is not None and len(via) < 4:
                via.append(f.f_code.co_qualname)
                f = f.f_back
            seen.append(Seen(SITE_OF.get(where), where, tuple(via), _describe(kwargs)))
        return real(*args, **kwargs)

    patched = set()
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not mod_name.startswith(("apps", "meshychat", "tools")):
            continue
        for attr, value in list(vars(mod).items()):
            if value is real:
                monkeypatch.setattr(mod, attr, _resource_spy)
                patched.add(f"{mod_name}.{attr}")
    # The spy's positive control: every module-level binding the census knows is patched.
    assert {"apps.memoryskills.permissions.check_permission",
            "apps.memoryskills.check_permission",
            "apps.memoryskills.handlers.check_permission",
            "apps.memoryskills.redact.check_permission", "apps.memoryskills.edit.check_permission",
            "apps.mesh.views.check_permission", "apps.meshsem.views.check_permission",
            "apps.meshsem.send_hints.check_permission",
            "apps.wsgw.mesh_notify.check_permission",
            "apps.meshsum.views.check_permission"} <= patched, patched
    return seen


def _got(seen: list[Seen]) -> list[tuple]:
    return [(s.site, s.resource) for s in seen]


def _assert_seen(seen: list[Seen], expected: list[tuple]) -> None:
    """Exactly these (site, resource) calls, as a multiset, and none from a caller outside
    the table. The kind is compared by exact type in `_describe`, then the string."""
    unknown = [s for s in seen if s.site is None]
    assert not unknown, [(s.where, s.resource) for s in unknown]
    assert collections.Counter(_got(seen)) == collections.Counter(expected), (_got(seen),
                                                                              expected)


WIDE = ("namespace_wide",)


def test_sites_2_3_4_5_handlers_write_read_query_list_pass_the_key_or_prefix(
        owner, app_conn, spy):
    """memory.write and memory.read pass Key(the key); memory.query passes
    Prefix(key_prefix), and Prefix("") without one; memory.list with a namespace passes
    Prefix(prefix), and Prefix("") without one. memory.list WITHOUT a namespace calls no
    check_permission (it enumerates by the set form of `meshy_namespace_permission`, design
    3.5 and divergence 040 slice 3): the negative beside the positives. Controls: every call
    is answered, as the owner, the real way."""
    writer = _peer(owner, "writer")
    ns = _claim(owner, writer)
    key = f"t2/w/{uuid.uuid4().hex[:8]}"

    wrote = _dispatch(app_conn, writer, "memory.write",
                      {"namespace": ns, "key": key, "value": {"t2": 1}})
    assert not isinstance(wrote, Exception), wrote
    _assert_seen(spy, [(2, ("key", key))])

    spy.clear()
    read = _dispatch(app_conn, writer, "memory.read", {"namespace": ns, "key": key})
    assert not isinstance(read, Exception) and read["memo"]["key"] == key, read
    _assert_seen(spy, [(3, ("key", key))])

    for args, want in (({"key_prefix": "t2/w/"}, ("prefix", "t2/w/")),
                       ({}, ("prefix", "")), ({"key_prefix": ""}, ("prefix", ""))):
        spy.clear()
        got = _dispatch(app_conn, writer, "memory.query", {"namespace": ns, **args})
        assert not isinstance(got, Exception), got
        _assert_seen(spy, [(4, want)])

    for args, want in (({"prefix": "t2/"}, ("prefix", "t2/")), ({}, ("prefix", "")),
                       ({"prefix": ""}, ("prefix", ""))):
        spy.clear()
        got = _dispatch(app_conn, writer, "memory.list", {"namespace": ns, **args})
        assert not isinstance(got, Exception), got
        _assert_seen(spy, [(5, want)])

    spy.clear()
    got = _dispatch(app_conn, writer, "memory.list", {})
    assert not isinstance(got, Exception), got
    assert spy == [], "memory.list without a namespace is not a check_permission site"


def test_site_2_a_refused_write_still_names_its_key(owner, app_conn, spy):
    """A caller without a grant is refused at the check, and the check still named the key:
    the resource is passed before the decision, not only on the way to a success.
    Control: the owner's write to the same key lands."""
    writer, stranger = _peer(owner, "w-owner"), _peer(owner, "w-stranger")
    ns = _claim(owner, writer)
    key = f"t2/refused/{uuid.uuid4().hex[:8]}"
    refused = _dispatch(app_conn, stranger, "memory.write",
                        {"namespace": ns, "key": key, "value": {"t2": 2}})
    assert getattr(refused, "code", None) == "E_PERMISSION_DENIED", refused
    _assert_seen(spy, [(2, ("key", key))])
    spy.clear()
    assert not isinstance(_dispatch(app_conn, writer, "memory.write",
                                    {"namespace": ns, "key": key, "value": {"t2": 2}}),
                          Exception)
    _assert_seen(spy, [(2, ("key", key))])


def test_sites_1_and_6_grant_is_namespace_wide_and_prune_names_its_key(owner, app_conn, spy):
    """memory.grant passes NAMESPACE_WIDE. memory.prune passes Key(key) when it is given one
    (the empty string included: its SQL is `key = ''`, one key, not every key) and
    NAMESPACE_WIDE only when no key is given (`%s::text IS NULL`, every key). Controls: the
    grant lands; prune is answered by its own retention rule, after the check."""
    admin = _peer(owner, "g-admin")
    grantee = _peer(owner, "g-grantee", group=admin["group"])
    ns = _claim(owner, admin)
    granted = _dispatch(app_conn, admin, "memory.grant",
                        {"namespace": ns, "grantee_peer_id": str(grantee["peer_id"]),
                         "permission": "read"})
    assert not isinstance(granted, Exception), granted
    _assert_seen(spy, [(1, WIDE)])

    for args, want in (({"key": "t2/p/1"}, ("key", "t2/p/1")), ({"key": ""}, ("key", "")),
                       ({}, WIDE)):
        spy.clear()
        pruned = _dispatch(app_conn, admin, "memory.prune", {"namespace": ns, **args})
        assert getattr(pruned, "code", None) == "E_BAD_REQUEST", pruned   # keep_all
        assert "high_churn" in pruned.message, pruned.message
        _assert_seen(spy, [(6, want)])


def test_site_7_the_graph_routes_and_the_addressed_wait_are_namespace_wide(owner, spy):
    """require_mesh_read passes NAMESPACE_WIDE, reached from the graph routes' `_route`
    (GET /v1/mesh/inbox) and from the addressed wait's `_mesh_read_refusal`. Controls: a
    reader is answered (200 or 204), a stranger is refused 403 by the same check."""
    reader, stranger = _peer(owner, "g7-reader"), _peer(owner, "g7-stranger")
    _mesh_grant(owner, reader)
    client = Client()

    r = client.get("/v1/mesh/inbox", **_auth(reader["peer_id"]))
    assert r.status_code == 200, (r.status_code, r.content[:300])
    _assert_seen(spy, [(7, WIDE)])
    assert "_route.<locals>.decorate.<locals>.wrapper" in spy[0].via, spy[0].via

    spy.clear()
    r = client.get("/v1/mesh/wait?addressed=1&timeout=0", **_auth(reader["peer_id"]))
    assert r.status_code in (200, 204), (r.status_code, r.content[:300])
    _assert_seen(spy, [(7, WIDE)])
    assert spy[0].via[0] == "_mesh_read_refusal", spy[0].via

    spy.clear()
    r = client.get("/v1/mesh/inbox", **_auth(stranger["peer_id"]))
    assert r.status_code == 403, (r.status_code, r.content[:300])
    _assert_seen(spy, [(7, WIDE)])


def test_site_8_the_semantic_routes_gate_is_namespace_wide(owner, spy):
    """meshsem's `_route` passes NAMESPACE_WIDE (GET /v1/mesh/search). Control: a stranger
    is refused 403 by that check; a reader is let past it to the handler."""
    reader, stranger = _peer(owner, "s8-reader"), _peer(owner, "s8-stranger")
    _mesh_grant(owner, reader)
    client = Client()
    # An unknown space: past the gate the handler answers 503 E_SPACE_UNKNOWN at once, and
    # no model host is ever asked.
    with override_settings(MESHY_SEMANTIC_SPACE="t2-no-such-space", MESHY_MODEL_PROVIDERS=[]):
        r = client.get("/v1/mesh/search?q=t2", **_auth(stranger["peer_id"]))
        assert r.status_code == 403, (r.status_code, r.content[:300])
        _assert_seen(spy, [(8, WIDE)])
        spy.clear()
        r = client.get("/v1/mesh/search?q=t2", **_auth(reader["peer_id"]))
        assert r.status_code == 503 and b"E_SPACE_UNKNOWN" in r.content, (r.status_code,
                                                                          r.content[:300])
        _assert_seen(spy, [(8, WIDE)])


def test_sites_9_and_11_the_push_names_the_rows_key(owner, memory_authority, spy):
    """The push: resolve_recipients passes Key(nudge.key) (site 9), and the preview's gate
    (push_frame -> preview_gate -> mesh_notify.body_withheld -> search.body_withheld) passes
    Key(nudge.key) too (site 11). Controls: the reader is a recipient, role `to`, and its
    frame carries the preview."""
    from apps.core import rls
    from apps.wsgw import mesh_notify

    author = _peer(owner, "p-author")
    reader = _peer(owner, "p-reader", group=memory_authority["group"])
    _mesh_grant(owner, reader)
    key = _mesh_row(owner, author, {"to": [reader["name"]], "body": "t2 push preview"})
    with rls.server_internal(owner):
        nudge = mesh_notify.load_nudge(owner, key, author["peer_id"])
        recipients, _skipped = mesh_notify.resolve_recipients(owner, nudge, [reader["peer_id"]])
    assert recipients == ((reader["peer_id"], "to"),), recipients
    _assert_seen(spy, [(9, ("key", key))])

    spy.clear()
    with rls.server_internal(owner):
        frame, withheld = mesh_notify.push_frame(owner, nudge, reader["peer_id"], "to", None)
    assert withheld is None and frame.get("preview_unverified") == "t2 push preview", frame
    _assert_seen(spy, [(11, ("key", key))])
    assert spy[0].via[:4] == ("_anchor_body_withheld", "body_withheld", "preview_gate",
                              "push_frame"), spy[0].via


def test_site_10_the_revocation_check_is_namespace_wide(owner, spy):
    """has_mesh_read passes NAMESPACE_WIDE. Controls: True for a reader, False for a
    stranger, by the same check."""
    from apps.wsgw import revocation

    reader, stranger = _peer(owner, "r10-reader"), _peer(owner, "r10-stranger")
    _mesh_grant(owner, reader)
    assert revocation.has_mesh_read(owner, reader["peer_id"]) is True
    assert revocation.has_mesh_read(owner, stranger["peer_id"]) is False
    _assert_seen(spy, [(10, WIDE), (10, WIDE)])


def test_site_11_the_addressed_wait_asks_the_gate_for_each_rows_own_key(
        owner, memory_authority, spy):
    """The addressed wait's preview: the gate is asked with EACH gated row's own key, not
    the first row's for all and not namespace-wide. Controls: both frames come back with
    their previews; a row with nothing gated (no string body, no cc) asks no gate."""
    author = _peer(owner, "wa-author")
    reader = _peer(owner, "wa-reader", group=memory_authority["group"])
    _mesh_grant(owner, reader)
    client = Client()
    r = client.get("/v1/mesh/wait?addressed=1&timeout=0", **_auth(reader["peer_id"]))
    assert r.status_code in (200, 204), r.status_code
    since = r["X-Meshy-Cursor"] if r.status_code == 204 else json.loads(r.content)["cursor"]

    first = _mesh_row(owner, author, {"to": [reader["name"]], "body": "t2 first"})
    second = _mesh_row(owner, author, {"to": [reader["name"]], "body": "t2 second"})
    bare = _mesh_row(owner, author, {"to": [reader["name"]], "body": {"not": "a string"}})
    spy.clear()
    r = client.get(f"/v1/mesh/wait?addressed=1&timeout=0&since={since}",
                   **_auth(reader["peer_id"]))
    assert r.status_code == 200, (r.status_code, r.content[:300])
    previews = {m["key"]: m.get("preview_unverified") for m in json.loads(r.content)["messages"]}
    assert previews == {first: "t2 first", second: "t2 second", bare: None}, previews
    _assert_seen(spy, [(7, WIDE), (11, ("key", first)), (11, ("key", second))])


def test_site_11_the_context_anchor_names_its_own_key(owner, memory_authority, spy,
                                                      monkeypatch):
    """GET /v1/mesh/context: the route's gate is site 8 (NAMESPACE_WIDE) and the anchor's
    body gate is site 11 with Key(the anchor's key), asked from `context_for`. Control:
    the anchor's body is shown to this reader."""
    from apps.meshsem import search, spaces
    from tests.fixtures import meshsem_db as db
    from tests.fixtures.meshsem_fake import FakeModelHost

    space = db.make_space("t2")
    monkeypatch.setitem(spaces.SPACES, space.name, space)
    embed, rerank = FakeModelHost(dims=db.DIMS), FakeModelHost(dims=db.DIMS)
    try:
        providers = [{"id": "embed-hqb", "kind": "embed", "base_url": embed.base_url,
                      "space": space.name},
                     {"id": "rerank-hqb", "kind": "rerank", "base_url": rerank.base_url}]
        with override_settings(MESHY_SEMANTIC_SPACE=space.name,
                               MESHY_MODEL_PROVIDERS=providers,
                               MESHY_EMBED_FORBIDDEN_ROWS=None):
            search.forget()
            author = _peer(owner, "c-author")
            reader = _peer(owner, "c-reader", group=memory_authority["group"])
            _mesh_grant(owner, reader)
            key = _mesh_row(owner, author, {"body": "t2 anchor body"})
            spy.clear()
            r = Client().get(f"/v1/mesh/context?key={key}&rerank=0", **_auth(reader["peer_id"]))
            assert r.status_code == 200, (r.status_code, r.content[:300])
            anchor = r.json()["anchor"]
            assert anchor["body"] == "t2 anchor body" and anchor["body_withheld"] is None, anchor
            _assert_seen(spy, [(8, WIDE), (11, ("key", key))])
            gate = next(s for s in spy if s.site == 11)
            assert gate.via[:2] == ("_anchor_body_withheld", "context_for"), gate.via
    finally:
        search.forget()
        embed.close()
        rerank.close()


def test_site_11_the_addressed_wait_asks_the_readers_gates_once_per_page(
        owner, memory_authority, spy, monkeypatch):
    """F2 of the slice-2 review: a page of gated rows asks the half of 027's gate that
    depends on the reader alone (DPoP possession, then the pipeline's skill, key and tenancy
    gates, `pipeline.refusal_without_envelope`) ONCE, and the namespace predicate once per
    row key. Slice 2 as built asked the whole gate per row: a full page (ROW_LIMIT, 50) was
    50 runs of the reader's gates, measured at about 0.5 s a page more than one. Controls:
    the three previews are shown, and the namespace predicate is asked about each key."""
    from apps.calls import pipeline

    author = _peer(owner, "once-author")
    reader = _peer(owner, "once-reader", group=memory_authority["group"])
    _mesh_grant(owner, reader)
    client = Client()
    r = client.get("/v1/mesh/wait?addressed=1&timeout=0", **_auth(reader["peer_id"]))
    assert r.status_code in (200, 204), r.status_code
    since = r["X-Meshy-Cursor"] if r.status_code == 204 else json.loads(r.content)["cursor"]
    keys = [_mesh_row(owner, author, {"to": [reader["name"]], "body": f"t2 once {n}"})
            for n in range(3)]

    real = pipeline.refusal_without_envelope
    asked: list = []

    def counted(conn, **kwargs):
        asked.append(str(kwargs["caller_peer_id"]))
        return real(conn, **kwargs)

    monkeypatch.setattr(pipeline, "refusal_without_envelope", counted)
    spy.clear()
    r = client.get(f"/v1/mesh/wait?addressed=1&timeout=0&since={since}",
                   **_auth(reader["peer_id"]))
    assert r.status_code == 200, (r.status_code, r.content[:300])
    previews = {m["key"]: m.get("preview_unverified") for m in json.loads(r.content)["messages"]}
    assert previews == {k: f"t2 once {n}" for n, k in enumerate(keys)}, previews
    assert asked == [str(reader["peer_id"])], asked
    _assert_seen(spy, [(7, WIDE)] + [(11, ("key", k)) for k in keys])


def test_site_11_each_rows_preview_follows_its_own_gate_answer(owner, memory_authority,
                                                               monkeypatch):
    """F3 of the slice-2 review: the addressed wait decides each row's preview and `cc` by
    THAT row's answer. The namespace predicate is made to refuse exactly the second of three
    keys, a stand-in for slice 4's key-scoped decision (today every key gets the same
    answer, which is why no test held this and a page-wide `any` or `all` survived): the
    first and third frames carry their preview and `cc`, the second carries neither.
    Controls: all three frames come back; the predicate was asked about each key once, in
    page order; with nothing refused, the second's preview is shown."""
    from apps.memoryskills import permissions as perm
    from apps.memoryskills.errors import HandlerError

    author = _peer(owner, "own-author")
    reader = _peer(owner, "own-reader", group=memory_authority["group"])
    _mesh_grant(owner, reader)
    client = Client()
    r = client.get("/v1/mesh/wait?addressed=1&timeout=0", **_auth(reader["peer_id"]))
    assert r.status_code in (200, 204), r.status_code
    since = r["X-Meshy-Cursor"] if r.status_code == 204 else json.loads(r.content)["cursor"]
    first, second, third = (
        _mesh_row(owner, author, {"to": [reader["name"]], "cc": ["t2-copied"],
                                  "body": f"t2 own {n}"}) for n in ("first", "second", "third"))

    def frames():
        r = client.get(f"/v1/mesh/wait?addressed=1&timeout=0&since={since}",
                       **_auth(reader["peer_id"]))
        assert r.status_code == 200, (r.status_code, r.content[:300])
        return {m["key"]: (m.get("preview_unverified"), m.get("cc"))
                for m in json.loads(r.content)["messages"]}

    assert frames()[second] == ("t2 own second", ["t2-copied"]), "control: nothing refused"

    real = perm.check_permission
    asked: list = []

    def refuses_the_second(namespace, peer_id, need, conn, *, resource):
        if type(resource) is perm.Key:
            asked.append(resource.k)
            if resource.k == second:
                raise HandlerError("E_PERMISSION_DENIED", "t2: this one key is refused")
        return real(namespace, peer_id, need, conn, resource=resource)

    # A function-local import reads the module's attribute at call time: 027's gate.
    monkeypatch.setattr(perm, "check_permission", refuses_the_second)
    got = frames()
    assert asked == [first, second, third], asked
    assert got == {first: ("t2 own first", ["t2-copied"]), second: (None, None),
                   third: ("t2 own third", ["t2-copied"])}, got


def test_sites_12_and_13_the_summaries_gates_are_namespace_wide(owner, tmp_path, monkeypatch,
                                                                spy):
    """GET /v1/mesh/summaries/thread (divergence 037, on main since 228a6ab): the route's
    gate is site 12, NAMESPACE_WIDE, site 8's question; the body gate in the transaction
    that reads the window, and again in step 10's re-check after the model call, is site 13,
    027's gate asked about the namespace (NAMESPACE_WIDE), since it is asked before the rows
    it covers are read. Controls: a reader gets its 200 summary; a stranger is refused 403
    by site 12 alone, with no model request made."""
    from tests.fixtures.meshsum_world import SummaryWorld, bearer

    _mesh_ns(owner)
    world = SummaryWorld(DSN, tmp_path, owner, monkeypatch)
    try:
        reader = world.reader("t2-sum")
        stranger = _peer(owner, "sum-stranger")
        thread, _rows = world.thread(2)
        asked_before = world.fake_requests()
        spy.clear()
        r = Client().get("/v1/mesh/summaries/thread", {"thread_id": thread},
                         **bearer(stranger["peer_id"]))
        assert r.status_code == 403, (r.status_code, r.content[:300])
        _assert_seen(spy, [(12, WIDE)])
        assert world.fake_requests() == asked_before

        spy.clear()
        r = Client().get("/v1/mesh/summaries/thread", {"thread_id": thread},
                         **bearer(reader["peer_id"]))
        assert r.status_code == 200, (r.status_code, r.content[:300])
        body = r.json()
        assert body["tier"] == "generated" and len(body["covered"]) == 2, body
        assert body["provenance"]["provider_id"] == "chat-fake", body    # a model call made
        _assert_seen(spy, [(12, WIDE), (13, WIDE), (13, WIDE)])
        assert [s.via[0] for s in spy if s.site == 13] == ["_admitted", "_admitted"], spy
    finally:
        world.close()


def test_site_14_the_redaction_names_the_rows_key(owner, app_conn, spy):
    """memory.redact passes Key(the key) for its write permission (site 14). Controls: the redaction is answered, as the
    owner, the real way (the answer names the version it withdrew), and the check named the key BEFORE the decision, so a
    caller without the grant is refused with the key named too."""
    writer, stranger = _peer(owner, "rd-owner"), _peer(owner, "rd-stranger")
    ns = _claim(owner, writer)
    key = f"t2/redact/{uuid.uuid4().hex[:8]}"
    wrote = _dispatch(app_conn, writer, "memory.write", {"namespace": ns, "key": key, "value": {"t2": 14}})
    assert not isinstance(wrote, Exception), wrote

    spy.clear()
    refused = _dispatch(app_conn, stranger, "memory.redact",
                        {"namespace": ns, "key": key, "reason_code": "sent_in_error"})
    assert getattr(refused, "code", None) == "E_PERMISSION_DENIED", refused
    _assert_seen(spy, [(14, ("key", key))])

    spy.clear()
    done = _dispatch(app_conn, writer, "memory.redact",
                     {"namespace": ns, "key": key, "reason_code": "sent_in_error"})
    assert not isinstance(done, Exception) and [r["version"] for r in done["redacted"]] == [1], done
    _assert_seen(spy, [(14, ("key", key))])


def test_site_15_the_redaction_push_names_the_rows_key(owner, memory_authority, spy):
    """The push of a redaction's event: resolve_redaction_recipients passes Key(red.nudge.key), the same gate a message
    frame's recipients pass (site 9), for the row's own key (site 15). Controls: the reader is a recipient of the row and
    the event is for it (the tuple names it); the redacting seat is not told."""
    from psycopg.rows import dict_row

    from apps.core import rls
    from apps.memoryskills import redact
    from apps.wsgw import mesh_notify

    author = _peer(owner, "rp-author")
    reader = _peer(owner, "rp-reader", group=memory_authority["group"])
    _mesh_grant(owner, reader)
    ns = _mesh_ns(owner)
    key = _mesh_row(owner, author, {"to": [reader["name"]], "body": "t2 redaction push"})
    with owner.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT id, version, value, signed_bytes, payload_jcs_sha256, created_by_peer_id "
                    "FROM shared_memory WHERE namespace = %s AND key = %s", (ns, key))
        record = redact._redact_one(cur, cur.fetchone(), ns, key, author["peer_id"], author["owner_id"],
                                    "sent_in_error", False, b"the request as signed", f"call-{uuid.uuid4()}", [])
    spy.clear()
    with rls.server_internal(owner):
        red = mesh_notify.load_redaction(owner, uuid.UUID(record["redaction_id"]))
        recipients, skipped = mesh_notify.resolve_redaction_recipients(
            owner, red, [reader["peer_id"], author["peer_id"]])
    assert recipients == (reader["peer_id"],), (recipients, skipped)
    assert skipped.get("redactor") == 1, skipped
    _assert_seen(spy, [(15, ("key", key))])


def test_site_16_the_edit_names_the_rows_key(owner, app_conn, spy):
    """memory.edit passes Key(the key) for its write permission (site 16). Controls: the edit is answered, as the owner's seat,
    the real way (the answer names the version), and the check named the key BEFORE the decision, so a seat without the grant
    is refused with the key named too."""
    import time

    writer, stranger = _peer(owner, "ed-owner"), _peer(owner, "ed-stranger")
    ns = _mesh_ns(owner)
    _mesh_grant(owner, writer, "write")
    _mesh_grant(owner, stranger, "read")
    key = f"msg/{int(time.time() * 1000)}-{writer['name']}-{uuid.uuid4().hex[:6]}"
    value = {"schema_version": 1, "to": ["all"], "from": writer["name"], "type": "status", "thread_id": "t2/edit",
             "ts": "2026-09-30T00:00:00Z", "body": "t2 edit", "author_family": "claude"}
    wrote = _dispatch(app_conn, writer, "memory.write", {"namespace": ns, "key": key, "value": value})
    assert not isinstance(wrote, Exception), wrote

    spy.clear()
    args = {"namespace": ns, "key": key, "value": dict(value, body="t2 edited"), "expected_head_version": 1}
    refused = _dispatch(app_conn, stranger, "memory.edit", args)
    assert getattr(refused, "code", None) == "E_PERMISSION_DENIED", refused
    _assert_seen(spy, [(16, ("key", key))])

    spy.clear()
    done = _dispatch(app_conn, writer, "memory.edit", args)
    assert not isinstance(done, Exception) and done["version"] == 2, done
    _assert_seen(spy, [(16, ("key", key))])


def test_site_17_the_edit_push_names_the_rows_key(owner, app_conn, memory_authority, spy):
    """The push of an edit's event: resolve_edit_recipients passes Key(ed.nudge.key), the same gate a message frame's recipients
    pass (site 9), for the row's own key (site 17). Controls: the reader is a recipient of the row and the event is for it (the
    tuple names it, with its role); the editing seat is not told."""
    import time

    from apps.core import rls
    from apps.wsgw import mesh_notify

    writer = _peer(owner, "ep-writer")
    reader = _peer(owner, "ep-reader", group=memory_authority["group"])
    ns = _mesh_ns(owner)
    _mesh_grant(owner, writer, "write")
    _mesh_grant(owner, reader)
    key = f"msg/{int(time.time() * 1000)}-{writer['name']}-{uuid.uuid4().hex[:6]}"
    value = {"schema_version": 1, "to": [reader["name"]], "from": writer["name"], "type": "status", "thread_id": "t2/editpush",
             "ts": "2026-09-30T00:00:00Z", "body": "t2 edit push", "author_family": "claude"}
    wrote = _dispatch(app_conn, writer, "memory.write", {"namespace": ns, "key": key, "value": value})
    assert not isinstance(wrote, Exception), wrote
    done = _dispatch(app_conn, writer, "memory.edit",
                     {"namespace": ns, "key": key, "value": dict(value, body="t2 edit push, edited"), "expected_head_version": 1})
    assert not isinstance(done, Exception) and done["version"] == 2, done
    record_id = owner.execute("SELECT id FROM shared_memory_edit WHERE memory_id = %s", (done["id"],)).fetchone()[0]

    spy.clear()
    with rls.server_internal(owner):
        ed = mesh_notify.load_edit(owner, record_id)
        recipients, skipped = mesh_notify.resolve_edit_recipients(owner, ed, [reader["peer_id"], writer["peer_id"]])
    assert recipients == ((reader["peer_id"], "to"),), (recipients, skipped)
    assert skipped.get("editor") == 1, skipped
    _assert_seen(spy, [(17, ("key", key))])


def test_sites_18_and_19_the_pre_send_hints_gates_are_namespace_wide(
        owner, memory_authority, spy, monkeypatch):
    """POST /v1/mesh/hints: the route's gate on shared/mesh-chat is site 19 (NAMESPACE_WIDE),
    and evaluate_hints's _may_read asks site 18 (NAMESPACE_WIDE) for the card namespace and
    for the transcript-context namespace before consulting state flags. Controls: a stranger
    without mesh-chat read is refused 403 by site 19 alone; a mail-only reader gets 200 with
    permission_denied on both secondary namespaces; granting both flips their reasons past
    the permission check."""
    from apps.meshsem import send_hints, spaces, views
    from tests.fixtures import meshsem_db as db

    space = db.make_space("t2h")
    monkeypatch.setitem(spaces.SPACES, space.name, space)
    ns_owner = _peer(owner, "h-nsowner")
    card_ns = _claim(owner, ns_owner)
    ctx_ns = _claim(owner, ns_owner)
    reader = _peer(owner, "h-reader", group=memory_authority["group"])
    stranger = _peer(owner, "h-stranger")
    _mesh_grant(owner, reader)
    payload = json.dumps({"body": "a draft over forty characters so pre-send hints checks each namespace"})
    client = Client()
    send_hints.forget()
    views._HINTS_BOUND.forget()
    try:
        with override_settings(MESHY_SEND_HINTS_ENABLED=True,
                               MESHY_SEND_HINTS_INCLUDE_CONTEXT=False,
                               MESHY_CONTEXT_INDEX=False,
                               MESHY_CONTEXT_NAMESPACE=ctx_ns,
                               MESHY_CARDS_NAMESPACE=card_ns,
                               MESHY_GRAPH_NAMESPACES="shared/mesh-chat",
                               MESHY_SEMANTIC_SPACE=space.name,
                               MESHY_MODEL_PROVIDERS=[]):
            r = client.post("/v1/mesh/hints", data=payload,
                            content_type="application/json", **_auth(stranger["peer_id"]))
            assert r.status_code == 403, (r.status_code, r.content[:300])
            _assert_seen(spy, [(19, WIDE)])

            spy.clear()
            r = client.post("/v1/mesh/hints", data=payload,
                            content_type="application/json", **_auth(reader["peer_id"]))
            assert r.status_code == 200, (r.status_code, r.content[:300])
            sources = r.json()["sources"]
            assert sources["decisions"] == {"ran": False, "reason": "permission_denied"}, sources
            assert sources["transcript_context"] == {"ran": False, "reason": "permission_denied"}, sources
            _assert_seen(spy, [(19, WIDE), (18, WIDE), (18, WIDE)])

            _grant(owner, card_ns, reader, ns_owner, "read")
            _grant(owner, ctx_ns, reader, ns_owner, "read")
            spy.clear()
            r = client.post("/v1/mesh/hints", data=payload,
                            content_type="application/json", **_auth(reader["peer_id"]))
            assert r.status_code == 200, (r.status_code, r.content[:300])
            sources = r.json()["sources"]
            assert sources["decisions"] == {"ran": False, "reason": "not_walked"}, sources
            assert sources["transcript_context"] == {"ran": False, "reason": "disabled"}, sources
            _assert_seen(spy, [(19, WIDE), (18, WIDE), (18, WIDE)])
    finally:
        send_hints.forget()
        views._HINTS_BOUND.forget()


def test_every_site_in_the_table_is_driven_by_a_test_here():
    """The dynamic half's completeness, statically: each of the nineteen sites is named by a
    drive in this file, so a site added to the table without a drive fails here."""
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    driven = set()
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and fn.name.startswith("test_site"):
            for node in ast.walk(fn):
                if isinstance(node, ast.Tuple) and node.elts and \
                        isinstance(node.elts[0], ast.Constant) and \
                        isinstance(node.elts[0].value, int) and len(node.elts) == 2:
                    driven.add(node.elts[0].value)
    assert driven >= set(SITES), sorted(set(SITES) - driven)
