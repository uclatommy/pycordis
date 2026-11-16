"""Write the behavior inventory of an upstream clone to ``conformance/upstream.json``.

Usage::

    uv run python scripts/upstream_inventory.py <clone> [--out PATH] [--ledger-stubs]

The script reads ``<clone>/packages/core/{src,tests}/*.ts`` and the clone ``HEAD``. It
lists the items that the conformance ledger must review (test cases, declarations,
members, error messages, event names, fiber states) with a hash of each, and it never
writes upstream code: only IDs, hashes, and the vocabulary strings.

The standard library has no TypeScript parser, and the inventory needs only item
boundaries. A small tokenizer and a statement splitter find them, and any construct that
they cannot place raises ``ValueError`` with the file and the offset, so a new upstream
construct stops the sync instead of being skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Item:
    """One inventory item.

    Attributes:
        id: The stable ID, for example ``src/fiber.ts:Fiber._unload``.
        kind: ``case``, ``symbol``, ``member``, ``rest``, ``message``, ``event`` or
            ``state``.
        spans: The character ranges of the item in its file. A declaration that
            merges several statements, or whose members are separate items, has more
            than one range. A vocabulary item has none.
        text: The string of a vocabulary item (``message``, ``event``, ``state``);
            ``None`` for every other kind.
    """

    id: str
    kind: str
    spans: tuple[tuple[int, int], ...]
    text: str | None = None


@dataclass(frozen=True)
class Token:
    """A token that is not a comment.

    Attributes:
        kind: ``ident``, ``num``, ``str``, ``re``, ``tpl`` or ``punct``.
        text: The source text of the token.
        start: The offset of the first character.
        end: The offset after the last character.
        nl_before: ``True`` when a line break precedes the token.
        value: The unescaped content of a ``str`` token; ``None`` otherwise.
        parts: The unescaped literal parts of a ``tpl`` token, split at each
            ``${…}``; ``None`` otherwise.
    """

    kind: str
    text: str
    start: int
    end: int
    nl_before: bool
    value: str | None = None
    parts: tuple[str, ...] | None = None


# --------------------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------------------

_IDENT = re.compile(r"#?(?:[^\W\d]|\$)[\w$]*")
_NUMBER = re.compile(r"\d[\w]*(?:\.\d[\w]*)?")
_PUNCT = re.compile(
    r"=>|\.\.\.|===|!==|==|!=|<=|&&|\|\||\?\?|\?\.|\+\+|--|\+=|-=|\*=|/=|\*\*|[^\s\w]"
)
_REGEX_AFTER_KEYWORD = frozenset(
    "return typeof case do else in of instanceof void delete throw new yield "
    "await".split()
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _fail(path: str, offset: int, message: str) -> ValueError:
    return ValueError(f"{path}: offset {offset}: {message}")


def _unescape(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        char = raw[i]
        if char != "\\":
            out.append(char)
            i += 1
            continue
        escape = raw[i + 1]
        if escape in _ESCAPES:
            out.append(_ESCAPES[escape])
            i += 2
        elif escape == "x":
            out.append(chr(int(raw[i + 2 : i + 4], 16)))
            i += 4
        elif escape == "u" and raw[i + 2] == "{":
            close = raw.index("}", i)
            out.append(chr(int(raw[i + 3 : close], 16)))
            i = close + 1
        elif escape == "u":
            out.append(chr(int(raw[i + 2 : i + 6], 16)))
            i += 6
        elif escape == "\n":
            i += 2
        else:
            out.append(escape)
            i += 2
    return "".join(out)


def _regex_allowed(previous: Token | None) -> bool:
    if previous is None:
        return True
    if previous.kind == "punct":
        return previous.text not in (")", "]", "}")
    return previous.kind == "ident" and previous.text in _REGEX_AFTER_KEYWORD


def _scan_template(text: str, start: int, path: str) -> tuple[int, tuple[str, ...]]:
    """Scan the template literal at ``start``; return its end and its literal parts."""
    parts: list[str] = []
    literal_start = start + 1
    i = literal_start
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
        elif char == "`":
            parts.append(_unescape(text[literal_start:i]))
            return i + 1, tuple(parts)
        elif text.startswith("${", i):
            parts.append(_unescape(text[literal_start:i]))
            _, i = _scan(text, i + 2, True, path)
            literal_start = i
        else:
            i += 1
    raise _fail(path, start, "unterminated template literal")


def _scan_regex(text: str, start: int, path: str) -> int:
    i = start + 1
    in_class = False
    while i < len(text):
        char = text[i]
        if char == "\n":
            break
        if char == "\\":
            i += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            i += 1
            while i < len(text) and text[i].isalpha():
                i += 1
            return i
        i += 1
    raise _fail(path, start, "unterminated regular expression literal")


def _scan(text: str, i: int, nested: bool, path: str) -> tuple[list[Token], int]:
    """Tokenize from ``i``. In nested mode, stop after the ``}`` that closes ``${``."""
    tokens: list[Token] = []
    depth = 0
    newline = False
    while i < len(text):
        char = text[i]
        if char.isspace():
            newline = newline or char in "\n  "
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise _fail(path, i, "unterminated comment")
            newline = newline or "\n" in text[i:end]
            i = end + 2
            continue
        start = i
        value: str | None = None
        parts: tuple[str, ...] | None = None
        if char in "'\"":
            i += 1
            while i < len(text) and text[i] != char:
                if text[i] == "\n":
                    raise _fail(path, start, "unterminated string literal")
                i += 2 if text[i] == "\\" else 1
            if i >= len(text):
                raise _fail(path, start, "unterminated string literal")
            i += 1
            kind, value = "str", _unescape(text[start + 1 : i - 1])
        elif char == "`":
            i, parts = _scan_template(text, i, path)
            kind = "tpl"
        elif char == "/" and _regex_allowed(tokens[-1] if tokens else None):
            i = _scan_regex(text, i, path)
            kind = "re"
        elif match := _IDENT.match(text, i):
            i, kind = match.end(), "ident"
        elif match := _NUMBER.match(text, i):
            i, kind = match.end(), "num"
        else:
            match = _PUNCT.match(text, i)
            assert match is not None
            i, kind = match.end(), "punct"
            if nested and char == "{":
                depth += 1
            elif nested and char == "}":
                if depth == 0:
                    return tokens, i
                depth -= 1
        tokens.append(Token(kind, text[start:i], start, i, newline, value, parts))
        newline = False
    if nested:
        raise _fail(path, i, "unterminated template substitution")
    return tokens, i


def tokenize(text: str, path: str = "<text>") -> list[Token]:
    """Return the tokens of ``text`` without comments.

    A template literal is one token, whatever it holds. Raises ``ValueError`` with the
    file and the offset on text that the tokenizer cannot read.
    """
    return _scan(text, 0, False, path)[0]


def normalize(text: str) -> str:
    """Return ``text`` without comments and with one space between tokens.

    A reformat or a comment edit does not change the result; a changed token does.
    """
    return " ".join(token.text for token in tokenize(text))


def digest(text: str) -> str:
    """Return the SHA-256 of ``text`` as hexadecimal digits."""
    return hashlib.sha256(text.encode()).hexdigest()


# --------------------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------------------

_OPEN = ("(", "[", "{")
_CLOSE = (")", "]", "}")
# A statement continues over a line break after one of these tokens ...
_CONTINUES_AFTER = frozenset(
    ", = => . ?. ? : | & + - * / % ! ~ ^ && || ?? == === != !== += -= *= /= ... ** <= "
    "( [ { extends implements in instanceof as satisfies typeof keyof new await".split()
)
# ... and before one of these. `(` and `[` are not in the set: a line that starts with
# one starts a statement, as in a class body with a computed member name.
_CONTINUES_BEFORE = frozenset(
    ", = => . ?. ? : | & + - / % ^ && || ?? == === != !== += -= *= /= ** <= ) ] } "
    "extends implements in instanceof as satisfies else catch finally".split()
)
_DECLARATION_MODIFIERS = frozenset("export default declare abstract async".split())
_MEMBER_MODIFIERS = frozenset(
    "public private protected static readonly abstract override async get set declare "
    "accessor".split()
)
_TEST_CALLEES = frozenset("it test describe suite".split())


@dataclass(frozen=True)
class _Source:
    path: str
    text: str
    tokens: list[Token]
    match: dict[int, int]


def _parse(path: str, text: str) -> _Source:
    tokens = tokenize(text, path)
    match: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind != "punct":
            continue
        if token.text in _OPEN:
            stack.append(index)
        elif token.text in _CLOSE:
            if not stack or _CLOSE[_OPEN.index(tokens[stack[-1]].text)] != token.text:
                raise _fail(path, token.start, f"unbalanced {token.text!r}")
            opener = stack.pop()
            match[opener], match[index] = index, opener
    if stack:
        raise _fail(path, tokens[stack[-1]].start, "unclosed bracket")
    return _Source(path, text, tokens, match)


def _is(token: Token, *texts: str) -> bool:
    return token.kind in ("punct", "ident") and token.text in texts


def _splits(previous: Token, current: Token) -> bool:
    """Return ``True`` when a statement ends at the line break between the tokens."""
    if previous.kind in ("punct", "ident") and previous.text in _CONTINUES_AFTER:
        return False
    if previous.kind == "punct" and previous.text == ")" and _is(current, "{"):
        return False  # a body that starts on its own line
    return not (
        current.kind in ("punct", "ident") and current.text in _CONTINUES_BEFORE
    )


def _statements(source: _Source, lo: int, hi: int) -> list[tuple[int, int]]:
    """Split the tokens ``lo`` to ``hi`` into statements at depth 0."""
    tokens = source.tokens
    result: list[tuple[int, int]] = []
    start = lo
    depth = 0
    for i in range(lo, hi):
        token = tokens[i]
        if (
            depth == 0
            and i > start
            and token.nl_before
            and _splits(tokens[i - 1], token)
        ):
            result.append((start, i))
            start = i
        if token.kind != "punct":
            continue
        if token.text in _OPEN:
            depth += 1
        elif token.text in _CLOSE:
            depth -= 1
        elif token.text == ";" and depth == 0:
            result.append((start, i + 1))
            start = i + 1
    if start < hi:
        result.append((start, hi))
    return result


def _body(source: _Source, start: int, end: int) -> tuple[int, int]:
    """Return the token range inside the final ``{ }`` of the statement."""
    last = end - 1 if source.tokens[end - 1].text == ";" else end
    if not _is(source.tokens[last - 1], "}"):
        raise _fail(source.path, source.tokens[start].start, "declaration has no body")
    return source.match[last - 1] + 1, last - 1


@dataclass(frozen=True)
class _Declaration:
    kind: str  # symbol, class, enum or interface
    name: str


def _name_at(source: _Source, index: int, end: int) -> str:
    token = source.tokens[index] if index < end else None
    if token is None or token.kind != "ident":
        offset = source.tokens[end - 1].start if token is None else token.start
        raise _fail(source.path, offset, "declaration has no name")
    return token.text


def _classify(source: _Source, start: int, end: int) -> _Declaration | None:
    """Return the declaration that the statement makes, or ``None`` for other code."""
    tokens = source.tokens
    i = start
    modifiers: list[str] = []
    while (
        i < end
        and tokens[i].kind == "ident"
        and tokens[i].text in _DECLARATION_MODIFIERS
    ):
        modifiers.append(tokens[i].text)
        i += 1
    if i >= end:
        raise _fail(source.path, tokens[start].start, "statement has no declaration")
    word = tokens[i]
    nxt = tokens[i + 1] if i + 1 < end else None
    if word.kind != "ident":
        return None
    if word.text == "class":
        return _Declaration("class", _name_at(source, i + 1, end))
    if word.text == "interface":
        return _Declaration("interface", _name_at(source, i + 1, end))
    if word.text == "function":
        j = i + 2 if nxt is not None and _is(nxt, "*") else i + 1
        return _Declaration("symbol", _name_at(source, j, end))
    if word.text == "enum" or (
        word.text == "const" and nxt is not None and _is(nxt, "enum")
    ):
        j = i + 2 if word.text == "const" else i + 1
        return _Declaration("enum", _name_at(source, j, end))
    if word.text == "namespace" and nxt is not None and nxt.kind == "ident":
        return _Declaration("symbol", nxt.text)
    if word.text == "module" and nxt is not None and nxt.kind in ("str", "ident"):
        return _Declaration("symbol", f"module {nxt.value or nxt.text}")
    if word.text == "global" and "declare" in modifiers:
        return _Declaration("symbol", "global")
    if word.text == "type" and i + 2 < end and tokens[i + 1].kind == "ident":
        if _is(tokens[i + 2], "=", "<"):
            return _Declaration("symbol", tokens[i + 1].text)
    if word.text in ("const", "let", "var"):
        return _Declaration("symbol", _name_at(source, i + 1, end))
    if "default" in modifiers:
        raise _fail(source.path, word.start, "unnamed default export")
    return None


def _member_name(source: _Source, start: int, end: int) -> str | None:
    """Return the name of a method, accessor, or constructor; ``None`` for others."""
    tokens = source.tokens
    i = start
    while i < end:
        token = tokens[i]
        if _is(token, "@"):
            raise _fail(source.path, token.start, "decorator on a class member")
        if _is(token, "*"):
            i += 1
        elif token.kind == "ident" and token.text in _MEMBER_MODIFIERS and i + 1 < end:
            following = tokens[i + 1]
            if _is(following, "(", "<", "=", ":", ";", "?", "!"):
                break
            if token.text == "static" and _is(following, "{"):
                return None
            i += 1
        else:
            break
    if i >= end:
        return None
    token = tokens[i]
    if token.kind in ("ident", "num", "str"):
        name = token.value if token.kind == "str" else token.text
        i += 1
    elif _is(token, "["):
        close = source.match[i]
        name = "".join(t.text for t in tokens[i : close + 1])
        i = close + 1
    else:
        return None
    if i + 1 < end and _is(tokens[i], "?") and _is(tokens[i + 1], "("):
        i += 1
    if i < end and _is(tokens[i], "(", "<"):
        return name
    return None


def _enum_members(source: _Source, start: int, end: int) -> list[str]:
    lo, hi = _body(source, start, end)
    names: list[str] = []
    expecting = True
    for i in range(lo, hi):
        token = source.tokens[i]
        if expecting and token.kind in ("ident", "str"):
            names.append(token.value if token.kind == "str" else token.text)
        expecting = _is(token, ",")
    return names


# --------------------------------------------------------------------------------------
# Item scan
# --------------------------------------------------------------------------------------


def _title(source: _Source, index: int, end: int) -> str:
    token = source.tokens[index] if index < end else None
    if token is not None and token.kind == "str" and token.value is not None:
        return token.value
    if (
        token is not None
        and token.kind == "tpl"
        and token.parts
        and len(token.parts) == 1
    ):
        return token.parts[0]
    offset = source.tokens[end - 1].start if token is None else token.start
    raise _fail(source.path, offset, "test title is not a string literal")


def _each_rows(source: _Source, open_index: int) -> list[str]:
    """Return the formatted values of the literal table inside ``it.each( … )``."""
    tokens = source.tokens
    close = source.match[open_index]
    if not _is(tokens[open_index + 1], "["):
        raise _fail(
            source.path, tokens[open_index].start, "it.each table is not a literal"
        )
    table_close = source.match[open_index + 1]
    tail = [t.text for t in tokens[table_close + 1 : close]]
    if tail not in ([], ["as", "const"]):
        raise _fail(
            source.path, tokens[table_close].start, "it.each table is not a literal"
        )
    rows: list[str] = []
    element: list[Token] = []
    for token in [
        *tokens[open_index + 2 : table_close],
        Token("punct", ",", 0, 0, False),
    ]:
        if not _is(token, ","):
            element.append(token)
            continue
        if not element:
            continue
        rows.append(_row_text(source, element))
        element = []
    return rows


def _row_text(source: _Source, element: list[Token]) -> str:
    first = element[0]
    if len(element) == 1 and first.kind == "str" and first.value is not None:
        return first.value
    if len(element) == 1 and (first.kind == "num" or _is(first, "true", "false")):
        return first.text
    if len(element) == 2 and _is(first, "-") and element[1].kind == "num":
        return f"-{element[1].text}"
    raise _fail(source.path, first.start, "it.each row is not a primitive literal")


def _row_title(source: _Source, title: str, value: str, offset: int) -> str:
    placeholders = re.findall(r"%[A-Za-z#%]", title)
    if placeholders != ["%s"]:
        raise _fail(
            source.path, offset, f"it.each title needs exactly one %s: {title!r}"
        )
    return title.replace("%s", value, 1)


def _describe_body(source: _Source, call: int) -> tuple[int, int]:
    """Return the token range of the callback body of the call at ``call``."""
    tokens = source.tokens
    close = source.match[call + 1]
    i = call + 2
    while i < close:
        token = tokens[i]
        if _is(token, *_OPEN):
            i = source.match[i] + 1
            continue
        if _is(token, "=>") and _is(tokens[i + 1], "{"):
            return i + 2, source.match[i + 1]
        if _is(token, "function"):
            j = i + 1
            if _is(tokens[j], "("):
                j = source.match[j] + 1
            if _is(tokens[j], "{"):
                return j + 1, source.match[j]
        i += 1
    raise _fail(source.path, tokens[call].start, "describe has no callback body")


class _Collector:
    """Gather the token indices of each item; no token is claimed twice."""

    def __init__(self, source: _Source, spec: bool) -> None:
        self.source = source
        self.spec = spec
        self.items: dict[str, tuple[str, set[int]]] = {}
        self.claimed: set[int] = set()
        self.base = source.path.rsplit("/", 1)[-1]

    def add(self, id_: str, kind: str, indices: set[int]) -> None:
        if id_ in self.items:
            self.items[id_][1].update(indices)
        else:
            self.items[id_] = (kind, set(indices))

    def claim(self, id_: str, kind: str, indices: set[int]) -> None:
        if self.claimed & indices:
            first = self.source.tokens[min(self.claimed & indices)]
            raise _fail(self.source.path, first.start, f"{id_}: tokens claimed twice")
        self.add(id_, kind, indices)
        self.claimed |= indices

    # -- tests ---------------------------------------------------------------------

    def tests(self, lo: int, hi: int, titles: list[str]) -> None:
        for start, end in _statements(self.source, lo, hi):
            tokens = self.source.tokens
            first = tokens[start]
            if first.kind == "ident" and first.text in ("it", "describe"):
                self.test_statement(start, end, titles)
            else:
                self.reject_nested_tests(start, end)

    def reject_nested_tests(self, start: int, end: int) -> None:
        tokens = self.source.tokens
        for i in range(start, end - 1):
            token = tokens[i]
            before_dot = i > start and _is(tokens[i - 1], ".")
            if (
                token.kind == "ident"
                and token.text in _TEST_CALLEES
                and not before_dot
                and _is(tokens[i + 1], "(", ".")
            ):
                raise _fail(
                    self.source.path, token.start, "test call inside other code"
                )

    def test_statement(self, start: int, end: int, titles: list[str]) -> None:
        tokens = self.source.tokens
        callee, second = tokens[start], tokens[start + 1]
        path = self.source.path
        if _is(second, "."):
            if not (callee.text == "it" and _is(tokens[start + 2], "each")):
                raise _fail(
                    path,
                    second.start,
                    f"unsupported {callee.text}.{tokens[start + 2].text}",
                )
            self.each_case(start, end, titles)
        elif not _is(second, "("):
            raise _fail(path, second.start, f"unsupported {callee.text} form")
        elif callee.text == "it":
            title = _title(self.source, start + 2, end)
            self.case(self.case_id(titles, title), set(range(start, end)), second.start)
        else:
            title = _title(self.source, start + 2, end)
            lo, hi = _describe_body(self.source, start)
            self.tests(lo, hi, [*titles, title])

    def case_id(self, titles: list[str], title: str) -> str:
        return "::".join([self.base, *titles, title])

    def case(self, id_: str, indices: set[int], offset: int) -> None:
        if id_ in self.items:
            raise _fail(self.source.path, offset, f"duplicate test ID {id_}")
        self.claim(id_, "case", indices)

    def each_case(self, start: int, end: int, titles: list[str]) -> None:
        tokens = self.source.tokens
        table_open = start + 3
        if not _is(tokens[table_open], "("):
            raise _fail(self.source.path, tokens[start].start, "it.each has no table")
        rows = _each_rows(self.source, table_open)
        call = self.source.match[table_open] + 1
        if call >= end or not _is(tokens[call], "("):
            raise _fail(
                self.source.path, tokens[start].start, "it.each has no test call"
            )
        title = _title(self.source, call + 1, end)
        indices = set(range(start, end))
        if not rows:
            raise _fail(
                self.source.path, tokens[start].start, "it.each table has no rows"
            )
        ids = [
            self.case_id(
                titles, _row_title(self.source, title, row, tokens[call].start)
            )
            for row in rows
        ]
        for id_ in ids:
            if id_ in self.items:
                raise _fail(
                    self.source.path, tokens[start].start, f"duplicate test ID {id_}"
                )
        self.claimed |= indices
        for id_ in ids:
            self.add(id_, "case", indices)

    # -- declarations --------------------------------------------------------------

    def top_level(self) -> None:
        source = self.source
        for start, end in _statements(source, 0, len(source.tokens)):
            first = source.tokens[start]
            if self.spec and first.kind == "ident" and first.text in ("it", "describe"):
                self.test_statement(start, end, [])
                continue
            declaration = _classify(source, start, end)
            if declaration is None:
                if self.spec:
                    self.reject_nested_tests(start, end)
                continue
            self.declare(declaration, start, end)

    def declare(self, declaration: _Declaration, start: int, end: int) -> None:
        path = self.source.path
        if declaration.kind == "class":
            self.members(declaration.name, start, end)
        indices = set(range(start, end)) - self.claimed
        self.claim(f"{path}:{declaration.name}", "symbol", indices)

    def members(self, class_name: str, start: int, end: int) -> None:
        lo, hi = _body(self.source, start, end)
        for member_start, member_end in _statements(self.source, lo, hi):
            name = _member_name(self.source, member_start, member_end)
            if name is not None:
                id_ = f"{self.source.path}:{class_name}.{name}"
                self.claim(id_, "member", set(range(member_start, member_end)))

    # -- result --------------------------------------------------------------------

    def result(self) -> list[Item]:
        tokens = self.source.tokens
        rest = set(range(len(tokens))) - self.claimed
        items = dict(self.items)
        if rest:
            items[f"{self.source.path}:<rest>"] = ("rest", rest)
        return [
            Item(id_, kind, _spans(tokens, indices))
            for id_, (kind, indices) in items.items()
        ]


def _spans(tokens: list[Token], indices: set[int]) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    run_start = previous = None
    for index in sorted(indices):
        if previous is None or index != previous + 1:
            if run_start is not None and previous is not None:
                spans.append((tokens[run_start].start, tokens[previous].end))
            run_start = index
        previous = index
    if run_start is not None and previous is not None:
        spans.append((tokens[run_start].start, tokens[previous].end))
    return tuple(spans)


def _scan_file(path: str, text: str, spec: bool) -> list[Item]:
    collector = _Collector(_parse(path, text), spec)
    collector.top_level()
    return collector.result()


def scan_source(path: str, text: str) -> list[Item]:
    """Return the ``symbol``, ``member`` and ``rest`` items of a source file.

    ``path`` is relative to ``packages/core`` and is the prefix of every ID.
    """
    return _scan_file(path, text, False)


def scan_spec(path: str, text: str) -> list[Item]:
    """Return the ``case``, ``symbol``, ``member`` and ``rest`` items of a test file.

    A case ID uses the file name, not the directory: ``fiber.spec.ts::Fiber::title``.
    """
    return _scan_file(path, text, True)


# --------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------


def _split_arguments(source: _Source, open_index: int) -> list[tuple[int, int]]:
    close = source.match[open_index]
    result: list[tuple[int, int]] = []
    start = open_index + 1
    i = start
    while i < close:
        token = source.tokens[i]
        if _is(token, *_OPEN):
            i = source.match[i]
        elif _is(token, ","):
            result.append((start, i))
            start = i + 1
        i += 1
    if start < close:
        result.append((start, close))
    return result


def _message_template(source: _Source, start: int, end: int) -> str:
    """Return the literal parts of an expression, with each other part as ``{}``."""
    tokens = source.tokens
    operands: list[list[Token]] = [[]]
    i = start
    while i < end:
        token = tokens[i]
        if _is(token, "+"):
            operands.append([])
        else:
            operands[-1].append(token)
            if _is(token, *_OPEN):
                operands[-1].extend(tokens[i + 1 : source.match[i] + 1])
                i = source.match[i]
        i += 1
    pieces: list[str] = []
    for operand in operands:
        if len(operand) == 1 and operand[0].kind == "str":
            pieces.append(operand[0].value or "")
        elif len(operand) == 1 and operand[0].kind == "tpl":
            pieces.append("{}".join(operand[0].parts or ()))
        else:
            pieces.append("{}")
    return "".join(pieces)


def _messages(source: _Source) -> list[str]:
    """Return the message templates of error constructions.

    A call counts when its callee name ends in ``Error`` (``new TypeError(…)``,
    ``createAccessError(…)``). The message is the first argument; ``AggregateError`` and
    ``CordisError`` take it as the second. A message without literal text is skipped.
    """
    tokens = source.tokens
    result: list[str] = []
    for i, token in enumerate(tokens[:-1]):
        if token.kind != "ident" or not token.text.endswith("Error"):
            continue
        if not _is(tokens[i + 1], "("):
            continue
        if i > 0 and _is(tokens[i - 1], "."):
            continue
        arguments = _split_arguments(source, i + 1)
        index = 1 if token.text in ("AggregateError", "CordisError") else 0
        if index >= len(arguments):
            continue
        template = _message_template(source, *arguments[index])
        if template.replace("{}", ""):
            result.append(template)
    return result


def _code_table_messages(source: _Source) -> list[str]:
    """Return the values of the ``Code`` table of ``CordisError``."""
    tokens = source.tokens
    result: list[str] = []
    for start, end in _statements(source, 0, len(tokens)):
        declaration = _classify(source, start, end)
        if declaration is None or declaration.name != "CordisError":
            continue
        lo, hi = _body(source, start, end)
        for inner_start, inner_end in _statements(source, lo, hi):
            first = [t.text for t in tokens[inner_start : inner_start + 3]]
            if "Code" not in first or "const" not in first:
                continue
            for i in range(inner_start, inner_end - 1):
                if _is(tokens[i], ":") and tokens[i + 1].kind == "str":
                    result.append(tokens[i + 1].value or "")
    return result


def _vocabulary_file(path: str, text: str) -> list[Item]:
    source = _parse(path, text)
    entries: dict[str, str] = {}
    for template in [*_messages(source), *_code_table_messages(source)]:
        entries[f"message:{template}"] = template
    for start, end in _statements(source, 0, len(source.tokens)):
        declaration = _classify(source, start, end)
        if declaration is None:
            continue
        if declaration.name == "Events" and declaration.kind == "interface":
            lo, hi = _body(source, start, end)
            for member_start, _ in _statements(source, lo, hi):
                token = source.tokens[member_start]
                if token.kind == "str" and (token.value or "").startswith("internal/"):
                    entries[f"event:{token.value}"] = token.value or ""
        if declaration.name == "FiberState" and declaration.kind == "enum":
            for name in _enum_members(source, start, end):
                entries[f"state:{name}"] = name
    return [Item(id_, id_.split(":", 1)[0], (), text) for id_, text in entries.items()]


def vocabulary(src: dict[str, str]) -> list[Item]:
    """Return the ``message``, ``event`` and ``state`` items of the source files.

    ``src`` maps the path of each source file to its text. An item holds its string in
    ``text``: a message template (``service "{}" has been registered at <{}>``), an
    event name, or a state name.
    """
    entries: dict[str, Item] = {}
    for path, text in sorted(src.items()):
        for item in _vocabulary_file(path, text):
            entries[item.id] = item
    return list(entries.values())


# --------------------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------------------


def item_hash(item: Item, text: str) -> str:
    """Return the hash of an item that was scanned from ``text``.

    A vocabulary item hashes its string. Any other item hashes the normalized text of
    its ranges, so that a comment edit or a reformat does not change the hash.
    """
    if item.text is not None:
        return digest(item.text)
    return digest(normalize("\n".join(text[start:end] for start, end in item.spans)))


def build_inventory(core: Path, revision: str) -> dict[str, object]:
    """Return the inventory of ``packages/core`` at ``core`` for ``revision``."""
    items: dict[str, dict[str, str]] = {}
    sources: dict[str, str] = {}

    def record(item: Item, text: str) -> None:
        if item.id in items:
            raise ValueError(f"duplicate item ID {item.id}")
        entry = {"kind": item.kind, "hash": item_hash(item, text)}
        if item.text is not None:
            entry["text"] = item.text
        items[item.id] = entry

    for folder, scan in (("src", scan_source), ("tests", scan_spec)):
        for file in sorted((core / folder).glob("*.ts")):
            path = f"{folder}/{file.name}"
            text = file.read_text(encoding="utf-8")
            if folder == "src":
                sources[path] = text
            for item in scan(path, text):
                record(item, text)
    for item in vocabulary(sources):
        record(item, "")
    return {"revision": revision, "items": dict(sorted(items.items()))}


def ledger_stubs(items: dict[str, object], reviewed: set[str]) -> str:
    """Return TOML ledger entries for the inventory items that ``reviewed`` lacks.

    Each entry has the status ``pending`` and no hash: a hash is copied only after a
    review.
    """
    blocks = []
    for item_id in sorted(items):
        if item_id in reviewed:
            continue
        key = json.dumps(item_id, ensure_ascii=False).replace("\x7f", "\\u007f")
        blocks.append(f'[items.{key}]\nstatus = "pending"\n')
    return "\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("clone", type=Path, help="path of the upstream clone")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "conformance"
        / "upstream.json",
        help="the inventory file to write",
    )
    parser.add_argument(
        "--ledger-stubs",
        action="store_true",
        help="print pending ledger entries for items that the ledger lacks",
    )
    parser.add_argument(
        "--ledger",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "conformance" / "ledger.toml",
        help="the ledger file that --ledger-stubs reads",
    )
    args = parser.parse_args(argv)
    revision = subprocess.run(
        ["git", "-C", str(args.clone), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    inventory = build_inventory(args.clone / "packages" / "core", revision)
    args.out.write_text(
        json.dumps(inventory, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    kinds: dict[str, int] = {}
    for entry in inventory["items"].values():  # type: ignore[union-attr]
        kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
    summary = f"{revision} {dict(sorted(kinds.items()))}"
    if args.ledger_stubs:
        ledger = tomllib.loads(args.ledger.read_text(encoding="utf-8"))
        print(summary, file=sys.stderr)
        print(ledger_stubs(inventory["items"], set(ledger.get("items", {}))))  # type: ignore[arg-type]
    else:
        print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
