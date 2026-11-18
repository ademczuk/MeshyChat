"""The CEL subset the existing expression policies actually use. Nothing more.

WHY A SUBSET AND NOT AN INTERPRETER. A general CEL implementation would accept a language
far wider than the policies stored in `meshy_guard_policies`. Every accepted construct whose
semantics this module has not verified would create an admission path.

WHAT THE SUBSET COVERS, and why exactly this much. The complete population of expression
literals in the existing policies and tests is:

    skill == 'harness/claude_code/Bash'
    skill == '<name>'
    skill == 'deploy_prod' && args.risk == 'high' && args.count == 3
    skill ==                    (a parse-error fixture)
    args.missing.value          (a runtime-error fixture)

So: identifiers, single-quoted strings, integers, `==`, `&&`, and dotted member access.
The grammar below adds `!=`, `||`, `!`, parentheses, double-quoted strings, floats,
`true`/`false`/`null` - the immediate closure of that set, small enough that each rule is
stated and tested here. EVERYTHING ELSE RAISES:

    arithmetic (+ - * / %), ordering (< <= > >=), `in`, ternary `?:`, indexing `a[b]`,
    list and map literals, function or macro calls (`has()`, `size()`, `startsWith()`,
    `x.all(...)`), comprehensions, and any character the tokeniser does not know.

`UnsupportedExpression` is raised at COMPILE time for those, so an attachment carrying
one is rejected by `validate_attachment_params` at POST time rather than surfacing as a
silent allow at evaluation time. This is the rule the task states and the one this file
exists to keep: **never silently allow**.

THREE SEMANTIC DECISIONS, stated rather than implied, because each is a place where this
subset could differ from a real CEL engine and a silent difference would be the defect:

  1. AN UNKNOWN IDENTIFIER RAISES. The five bound names are `skill`, `phase`,
     `caller_peer`, `target_peer`, `args`. A sixth name is a policy written against a
     context that does not exist.
 2. MEMBER ACCESS ON A MISSING KEY RAISES, and so does member access on a non-mapping.
     `args.missing.value` is a runtime error, which enforce-mode turns into Deny and
     observe-mode into Allow. It is a RUNTIME error, not a compile error: whether a key
     exists depends on the event.
  3. HETEROGENEOUS `==` RAISES rather than evaluating to false. CEL's own behaviour for a
     cross-type comparison has not been verified, so the subset refuses instead of
     guessing, which fails towards a deny. Same-class comparison is exact: bool/bool,
    number/number (int and float are one class, as in CEL), string/string, null/null,
    list/list, map/map.

`&&` and `||` short-circuit left to right and require bool operands. Commutative absorption
(`false && <error>` is `false` regardless of evaluation order) is NOT implemented. The
only difference from CEL is an expression CEL would evaluate that this module errors on,
and an error is a deny under enforcement.
"""
from __future__ import annotations

import re
from typing import Any, Mapping


class CelError(Exception):
    """Base for everything this module raises. Never caught into an allow."""


class UnsupportedExpression(CelError):
    """The expression uses a construct outside the implemented subset.

    Raised at COMPILE time. A policy carrying one is rejected when it is attached, so it
    can never reach evaluation and be mistaken for "no policy matched".
    """


class ExpressionSyntaxError(CelError):
    """The expression is not well-formed."""


class EvaluationError(CelError):
    """Well-formed, but it cannot be evaluated against THIS event.

    `gate._decision_for_error` turns it into Deny under enforce and Allow under observe.
    """


# --------------------------------------------------------------------------- tokens

#: Ordered: the two-character operators must be tried before the one-character ones,
#: or `!=` tokenises as `!` followed by `=`.
_TOKEN_RE = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<string>'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")
  | (?P<number>\d+\.\d+|\d+)
  | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<op>==|!=|&&|\|\||[!().])
  | (?P<other>.)
    """,
    re.VERBOSE,
)

#: Named so the refusal message can say WHAT was refused rather than "unexpected token".
_REFUSED = {
    "+": "arithmetic", "-": "arithmetic", "*": "arithmetic", "/": "arithmetic",
    "%": "arithmetic",
    "<": "ordering comparison", ">": "ordering comparison",
    "[": "indexing / list literals", "]": "indexing / list literals",
    "{": "map literals", "}": "map literals",
    "?": "the ternary operator", ":": "the ternary operator",
    ",": "argument lists", "&": "bitwise operators", "|": "bitwise operators",
    "=": "assignment", "~": "bitwise operators", "^": "bitwise operators",
}

#: `in` is a CEL operator and the rest are CEL macros. Named so the refusal is specific.
_REFUSED_WORDS = {
    "in": "the `in` operator",
    "has": "the has() macro",
    "size": "the size() function",
    "all": "the all() comprehension",
    "exists": "the exists() comprehension",
    "exists_one": "the exists_one() comprehension",
    "map": "the map() comprehension",
    "filter": "the filter() comprehension",
    "matches": "the matches() function",
    "startsWith": "the startsWith() function",
    "endsWith": "the endsWith() function",
    "contains": "the contains() function",
}

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "0": "\0"}


class _Token:
    __slots__ = ("kind", "value", "pos")

    def __init__(self, kind: str, value: Any, pos: int):
        self.kind = kind
        self.value = value
        self.pos = pos

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind} {self.value!r} @{self.pos}>"


def _unescape(raw: str) -> str:
    """Minimal string-escape handling: the escapes CEL shares with JSON, and no more.

    `\\uXXXX` is REFUSED rather than decoded. No expression in the measured population
    above uses one,
    and a half-right unicode escape decoder on a comparison gate is exactly the kind of
    quiet difference this module exists to avoid.
    """
    out = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(raw):
            raise ExpressionSyntaxError("string literal ends in a backslash")
        nxt = raw[i + 1]
        if nxt not in _ESCAPES:
            raise UnsupportedExpression(
                f"unsupported string escape '\\{nxt}': this subset implements only "
                f"{sorted(_ESCAPES)}"
            )
        out.append(_ESCAPES[nxt])
        i += 2
    return "".join(out)


def tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    pos = 0
    length = len(source)
    while pos < length:
        m = _TOKEN_RE.match(source, pos)
        if m is None:  # pragma: no cover - `other` matches any single char
            raise ExpressionSyntaxError(f"cannot tokenize at offset {pos}")
        pos = m.end()
        if m.lastgroup == "space":
            continue
        text = m.group()
        if m.lastgroup == "string":
            tokens.append(_Token("literal", _unescape(text[1:-1]), m.start()))
        elif m.lastgroup == "number":
            value = float(text) if "." in text else int(text)
            tokens.append(_Token("literal", value, m.start()))
        elif m.lastgroup == "ident":
            if text in _REFUSED_WORDS:
                raise UnsupportedExpression(
                    f"{_REFUSED_WORDS[text]} is outside the implemented CEL subset"
                )
            if text in ("true", "false"):
                tokens.append(_Token("literal", text == "true", m.start()))
            elif text == "null":
                tokens.append(_Token("literal", None, m.start()))
            else:
                tokens.append(_Token("ident", text, m.start()))
        elif m.lastgroup == "op":
            tokens.append(_Token("op", text, m.start()))
        else:
            what = _REFUSED.get(text)
            if what:
                raise UnsupportedExpression(
                    f"{what} ({text!r}) is outside the implemented CEL subset"
                )
            raise ExpressionSyntaxError(f"unexpected character {text!r} at offset {m.start()}")
    return tokens


# ------------------------------------------------------------------------------ AST
# Tuples rather than classes: the tree is five node kinds deep and a dataclass per kind
# would be more ceremony than the grammar.
#   ("lit", value)
#   ("var", name)
#   ("field", base, name)
#   ("not", operand)
#   ("eq" | "ne" | "and" | "or", left, right)


class Program:
    """A compiled expression."""

    __slots__ = ("ast",)

    def __init__(self, ast: tuple):
        self.ast = ast

    def execute(self, variables: Mapping[str, Any]) -> Any:
        return _eval(self.ast, variables)


class _Parser:
    def __init__(self, tokens: list[_Token]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> _Token | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _accept_op(self, *ops: str) -> str | None:
        tok = self.peek()
        if tok is not None and tok.kind == "op" and tok.value in ops:
            self.i += 1
            return tok.value
        return None

    def parse(self) -> tuple:
        node = self.parse_or()
        leftover = self.peek()
        if leftover is not None:
            raise ExpressionSyntaxError(
                f"trailing input at offset {leftover.pos}: {leftover.value!r}"
            )
        return node

    def parse_or(self) -> tuple:
        node = self.parse_and()
        while self._accept_op("||"):
            node = ("or", node, self.parse_and())
        return node

    def parse_and(self) -> tuple:
        node = self.parse_equality()
        while self._accept_op("&&"):
            node = ("and", node, self.parse_equality())
        return node

    def parse_equality(self) -> tuple:
        node = self.parse_unary()
        while True:
            op = self._accept_op("==", "!=")
            if op is None:
                return node
            node = ("eq" if op == "==" else "ne", node, self.parse_unary())

    def parse_unary(self) -> tuple:
        if self._accept_op("!"):
            return ("not", self.parse_unary())
        return self.parse_primary()

    def parse_primary(self) -> tuple:
        tok = self.peek()
        if tok is None:
            raise ExpressionSyntaxError("expression ends where a value was expected")
        if tok.kind == "op" and tok.value == "(":
            self.i += 1
            node = self.parse_or()
            if not self._accept_op(")"):
                raise ExpressionSyntaxError("unbalanced '('")
            return node
        if tok.kind == "literal":
            self.i += 1
            return ("lit", tok.value)
        if tok.kind == "ident":
            self.i += 1
            node: tuple = ("var", tok.value)
            while True:
                nxt = self.peek()
                if nxt is not None and nxt.kind == "op" and nxt.value == "(":
                    # `foo(...)`. There are no functions in this subset, and admitting
                    # one silently would mean admitting whatever it evaluates to.
                    raise UnsupportedExpression(
                        f"function call {tok.value!r}(...) is outside the implemented "
                        "CEL subset"
                    )
                if self._accept_op(".") is None:
                    return node
                field = self.peek()
                if field is None or field.kind != "ident":
                    raise ExpressionSyntaxError("'.' must be followed by a field name")
                self.i += 1
                node = ("field", node, field.value)
        raise ExpressionSyntaxError(f"unexpected {tok.value!r} at offset {tok.pos}")


def compile_expression(source: str) -> Program:
    """Compile an expression.

    Raise `UnsupportedExpression` for an out-of-subset construct and
    `ExpressionSyntaxError` for malformed input. Both are `CelError`; neither is ever
    turned into an allow by any caller in this package.
    """
    if not isinstance(source, str):
        raise ExpressionSyntaxError("expression must be a string")
    if not source.strip():
        raise ExpressionSyntaxError("expression is empty")
    return Program(_Parser(tokenize(source)).parse())


# ----------------------------------------------------------------------- evaluation

def _class_of(value: Any) -> str:
    """The CEL type class for the same-class rule on `==`.

    bool is checked BEFORE int on purpose: in Python `True == 1` and
    `isinstance(True, int)`, so without this ordering `args.flag == 1` would compare a
    bool against a number and silently succeed.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "map"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


def _require_bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise EvaluationError(
            f"{where} needs a bool operand, got {_class_of(value)}"
        )
    return value


def _eval(node: tuple, variables: Mapping[str, Any]) -> Any:
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "var":
        name = node[1]
        if name not in variables:
            raise EvaluationError(
                f"unknown identifier {name!r}; the bound names are "
                f"{sorted(variables)}"
            )
        return variables[name]
    if kind == "field":
        base = _eval(node[1], variables)
        field = node[2]
        if not isinstance(base, Mapping):
            raise EvaluationError(
                f"cannot read field {field!r} from a {_class_of(base)}"
            )
        if field not in base:
            # `args.missing.value` is a RUNTIME error.
            raise EvaluationError(f"no such field {field!r}")
        return base[field]
    if kind == "not":
        return not _require_bool(_eval(node[1], variables), "'!'")
    if kind == "and":
        # Short-circuit. Left must still be a bool, so `<non-bool> && x` errors rather
        # than being coerced.
        if not _require_bool(_eval(node[1], variables), "'&&'"):
            return False
        return _require_bool(_eval(node[2], variables), "'&&'")
    if kind == "or":
        if _require_bool(_eval(node[1], variables), "'||'"):
            return True
        return _require_bool(_eval(node[2], variables), "'||'")
    if kind in ("eq", "ne"):
        left = _eval(node[1], variables)
        right = _eval(node[2], variables)
        lc, rc = _class_of(left), _class_of(right)
        if lc != rc:
            raise EvaluationError(
                f"cannot compare {lc} with {rc}: this subset refuses heterogeneous "
                "equality rather than guessing CEL's answer (module docstring, note 3)"
            )
        equal = left == right
        return equal if kind == "eq" else not equal
    raise EvaluationError(f"unhandled node {kind!r}")  # pragma: no cover


def evaluate(source: str, variables: Mapping[str, Any]) -> Any:
    """Compile and run in one step. Used by tests; handlers compile once per evaluation."""
    return compile_expression(source).execute(variables)
