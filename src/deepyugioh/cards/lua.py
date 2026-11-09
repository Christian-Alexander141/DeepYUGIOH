"""A small Lua reader for card scripts: tokens, function bodies and expressions.

It is not a full Lua parser. It knows enough to

* strip comments and strings,
* find every `function s.name(...) ... end` (also `local function name`),
* split a body into statements-ish token runs and find calls like
  `Duel.SelectMatchingCard(tp, s.filter, tp, LOCATION_DECK, 0, 1, 1, nil)`,
* parse boolean expressions (`return c:IsLevelBelow(4) and (c:IsRace(RACE_WARRIOR) or ...)`)
  into a small tree.

Anything it cannot follow is reported, never guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(
    r"""
    (?P<num>0[xX][0-9a-fA-F]+|\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|~=|<=|>=|\.\.\.|\.\.|::|[-+*/%^#&|~<>=(){}\[\];:,.])
    """,
    re.X,
)
_LONG_BRACKET = re.compile(r"\[(=*)\[.*?\]\1\]", re.S)
_OPENERS = {"function", "if", "do", "repeat"}
_CLOSERS = {"end", "until"}


@dataclass(frozen=True)
class Tok:
    kind: str   # num | name | op | str
    text: str


def _strip(source: str) -> str:
    """Remove comments, replace strings by a placeholder (keeps token boundaries)."""
    out, i, n = [], 0, len(source)
    while i < n:
        ch = source[i]
        if source.startswith("--", i):
            m = _LONG_BRACKET.match(source, i + 2)
            if m:
                i = m.end()
                continue
            j = source.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and source[j] != ch:
                j += 2 if source[j] == "\\" else 1
            out.append(" __STR__ ")
            i = j + 1
            continue
        if ch == "[":
            m = _LONG_BRACKET.match(source, i)
            if m:
                out.append(" __STR__ ")
                i = m.end()
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def tokenize(source: str) -> list[Tok]:
    text = _strip(source)
    return [Tok(m.lastgroup, m.group()) for m in _TOKEN.finditer(text)]


@dataclass
class Function:
    name: str                # "s.filter", "s.initial_effect", "filter" (local)
    params: list[str]
    body: list[Tok]


def functions(toks: list[Tok]) -> dict[str, Function]:
    """All named functions, including those nested inside others (e.g. local helpers)."""
    out: dict[str, Function] = {}
    i, n = 0, len(toks)
    while i < n:
        if toks[i].text == "function" and i + 1 < n and toks[i + 1].kind == "name":
            j = i + 1
            name = [toks[j].text]
            while j + 2 < n and toks[j + 1].text in (".", ":") and toks[j + 2].kind == "name":
                name.append(toks[j + 1].text + toks[j + 2].text)
                j += 2
            j += 1
            if j < n and toks[j].text == "(":
                params, j = _params(toks, j)
                end = block_end(toks, j)
                out["".join(name)] = Function("".join(name), params, toks[j:end])
        i += 1
    return out


def anonymous_function(toks: list[Tok], start: int) -> tuple[Function, int] | None:
    """`function(c) ... end` starting at toks[start] == 'function'."""
    if toks[start].text != "function" or start + 1 >= len(toks) or toks[start + 1].text != "(":
        return None
    params, j = _params(toks, start + 1)
    end = block_end(toks, j)
    return Function("<anonymous>", params, toks[j:end]), end + 1


def _params(toks, j):
    """toks[j] == '('; returns (names, index after ')')."""
    params = []
    j += 1
    while j < len(toks) and toks[j].text != ")":
        if toks[j].kind == "name" or toks[j].text == "...":
            params.append(toks[j].text)
        j += 1
    return params, j + 1


def block_end(toks: list[Tok], start: int) -> int:
    """Index of the `end` that closes the function body starting at `start` (depth 1)."""
    depth = 1
    i = start
    while i < len(toks):
        t = toks[i]
        if t.kind == "name":
            if t.text in _OPENERS:
                # `for ... do` / `while ... do`: only `do` opens the block
                depth += 1
            elif t.text in _CLOSERS:
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return len(toks)


# ---- calls ------------------------------------------------------------------
@dataclass
class Call:
    name: str                       # dotted name, e.g. "Duel.SelectMatchingCard", "c:IsLevelBelow"
    args: list[list[Tok]]           # raw argument token runs
    index: int                      # position in the token list
    assigned_to: str | None = None  # `local g = Duel.Select...` -> "g"


def calls(toks: list[Tok], prefixes: tuple[str, ...] = ("Duel.",)) -> list[Call]:
    """Calls whose dotted name starts with one of `prefixes`, in source order."""
    out = []
    i, n = 0, len(toks)
    while i < n:
        if toks[i].kind == "name" and (i == 0 or toks[i - 1].text not in (".", ":")):
            j, name = _dotted(toks, i)
            if j < n and toks[j].text == "(" and name.startswith(prefixes):
                args, end = split_args(toks, j)
                assigned = None
                if i >= 2 and toks[i - 1].text == "=" and toks[i - 2].kind == "name":
                    assigned = toks[i - 2].text
                out.append(Call(name, args, i, assigned))
        i += 1
    return out


def _dotted(toks, i):
    parts = [toks[i].text]
    j = i + 1
    while j + 1 < len(toks) and toks[j].text in (".", ":") and toks[j + 1].kind == "name":
        parts.append(toks[j].text + toks[j + 1].text)
        j += 2
    return j, "".join(parts)


def split_args(toks: list[Tok], open_idx: int) -> tuple[list[list[Tok]], int]:
    """toks[open_idx] == '('. Returns (arguments, index of the matching ')')."""
    args, cur, depth = [], [], 0
    i = open_idx + 1
    while i < len(toks):
        t = toks[i]
        if t.text in ("(", "{", "["):
            depth += 1
        elif t.text in (")", "}", "]"):
            if depth == 0:
                if cur or args:
                    args.append(cur)
                return args, i
            depth -= 1
        elif t.text == "function" and t.kind == "name":
            end = block_end(toks, i + 1)
            cur.extend(toks[i:end + 1])
            i = end + 1
            continue
        if t.text == "," and depth == 0:
            args.append(cur)
            cur = []
        else:
            cur.append(t)
        i += 1
    return args, len(toks)


def text(toks: list[Tok]) -> str:
    out = ""
    for t in toks:
        if out and (t.kind in ("name", "num") and out[-1].isalnum() or out[-1] == "_" and t.kind == "name"):
            out += " "
        out += t.text
    return out


# ---- expressions --------------------------------------------------------------
@dataclass
class Node:
    op: str                         # or | and | not | cmp | call | name | num | arith | other
    args: list = field(default_factory=list)
    value: str = ""                 # call name / identifier / number / comparison operator


class _Parser:
    def __init__(self, toks: list[Tok]):
        self.t, self.i = toks, 0

    def peek(self, k=0):
        return self.t[self.i + k].text if self.i + k < len(self.t) else None

    def take(self):
        tok = self.t[self.i]
        self.i += 1
        return tok

    def expr(self):
        node = self.and_()
        parts = [node]
        while self.peek() == "or":
            self.take()
            parts.append(self.and_())
        return parts[0] if len(parts) == 1 else Node("or", parts)

    def and_(self):
        parts = [self.not_()]
        while self.peek() == "and":
            self.take()
            parts.append(self.not_())
        return parts[0] if len(parts) == 1 else Node("and", parts)

    def not_(self):
        if self.peek() == "not":
            self.take()
            return Node("not", [self.not_()])
        return self.cmp()

    def cmp(self):
        left = self.arith()
        if self.peek() in ("==", "~=", "<=", ">=", "<", ">"):
            op = self.take().text
            return Node("cmp", [left, self.arith()], op)
        return left

    def arith(self):
        left = self.primary()
        while self.peek() in ("+", "-", "*", "/", "|", "&", "%", ".."):
            op = self.take().text
            left = Node("arith", [left, self.primary()], op)
        return left

    def primary(self):
        p = self.peek()
        if p is None:
            raise SyntaxError("unexpected end")
        if p == "(":
            self.take()
            node = self.expr()
            if self.peek() != ")":
                raise SyntaxError("missing )")
            self.take()
            return self._suffix(node)
        if p == "-":
            self.take()
            return Node("arith", [Node("num", value="0"), self.primary()], "-")
        tok = self.take()
        if tok.kind == "num":
            return Node("num", value=tok.text)
        if tok.kind == "name":
            if tok.text in ("true", "false", "nil"):
                return Node("name", value=tok.text)
            return self._suffix(Node("name", value=tok.text))
        if tok.text == "{":
            depth = 1
            while self.i < len(self.t) and depth:
                depth += {"{": 1, "}": -1}.get(self.take().text, 0)
            return Node("other", value="{...}")
        raise SyntaxError(f"unexpected {tok.text!r}")

    def _suffix(self, node):
        while True:
            p = self.peek()
            if p in (".", ":") and self.peek(1) is not None and self.i + 1 < len(self.t) and self.t[self.i + 1].kind == "name":
                self.take()
                name = self.take().text
                node = Node("name", value=f"{_render(node)}{p}{name}")
            elif p == "(":
                args, end = split_args(self.t, self.i)
                self.i = end + 1
                node = Node("call", [parse_expr(a) for a in args], _render(node))
            elif p == "[":
                depth = 0
                while self.i < len(self.t):
                    t = self.take().text
                    depth += {"[": 1, "]": -1}.get(t, 0)
                    if depth == 0:
                        break
                node = Node("other", value=_render(node) + "[...]")
            else:
                return node


def _render(node: Node) -> str:
    if node.op in ("name", "num", "other"):
        return node.value
    if node.op == "call":
        return node.value + "(...)"
    return node.op


def parse_expr(toks: list[Tok]) -> Node:
    """Parse one expression; unparsable input becomes Node('other')."""
    if not toks:
        return Node("other", value="")
    if toks[0].text == "function":
        return Node("other", value="function")
    try:
        p = _Parser(toks)
        node = p.expr()
        if p.i != len(toks):
            return Node("other", value=text(toks))
        return node
    except (SyntaxError, IndexError):
        return Node("other", value=text(toks))


def returns(body: list[Tok]) -> list[list[Tok]]:
    """Token runs of every `return ...` statement in a body (top level of the function only)."""
    out = []
    i, n = 0, len(body)
    while i < n:
        t = body[i]
        if t.kind == "name" and t.text == "function":
            i = block_end(body, i + 1) + 1
            continue
        if t.kind == "name" and t.text == "return":
            j = i + 1
            run = []
            while j < n and not (body[j].kind == "name" and body[j].text in ("end", "else", "elseif", "return", "local")) \
                    and body[j].text != ";":
                if body[j].text == "(":
                    _, close = split_args(body, j)
                    run.extend(body[j:close + 1])
                    j = close + 1
                    continue
                run.append(body[j])
                j += 1
            out.append(run)
            i = j
            continue
        i += 1
    return out


def has_branches(body: list[Tok]) -> bool:
    return any(t.kind == "name" and t.text in ("if", "for", "while") for t in body)
