"""Tests of the upstream inventory scanner on inline TypeScript snippets."""

import importlib.util
import json
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    path = Path(__file__).resolve().parent.parent / "scripts" / "upstream_inventory.py"
    spec = importlib.util.spec_from_file_location("upstream_inventory", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = (
        module  # dataclasses resolves annotations through sys.modules
    )
    spec.loader.exec_module(module)
    return module


inventory = _load_script()

NESTED = """\
import { describe, it } from 'vitest'
describe('A', () => {
  const shared = 1
  describe('B', () => {
    it('x', () => {})
  })
  it('y', async () => {
    expect(shared).to.equal(1)
  })
})
"""

EACH = """\
describe('A', () => {
  it.each(['p', 'q'])('does %s', async (mode) => {
    expect(mode).to.be.a('string')
  })
  it.each([false, true] as const)(
    'flag (%s)',
    () => {},
  )
})
"""

BRACES = """\
describe('A', () => {
  it('braces', () => {
    const text = '}' + "{" + `}${'{'}${ { a: '}' }.a }`
    const pattern = /[}\\/]{2}/g
    return text.length / 2 / pattern.lastIndex
  })
})
const after = 1
"""

CLASS = """\
export class Foo<T> extends Base<{ a: T }> {
  x = 1
  static { init() }
  get size() { return 1 }
  foo(a: string): void
  foo(a: number): void
  foo(a: any) {}
  [symbols.root]() {}
  [key: string]: any
  constructor(public a: number) {}
}
export namespace Foo {
  export const y = 1
}
"""

DECLARATIONS = """\
import { a } from './a.ts'
export type Mode = 'a' | 'b'
export interface Options { debug?: boolean }
declare module './context.ts' {
  interface Context { fiber: Fiber }
}
export const enum State { A, B }
export function* gen() {}
export async function load() {}
export const value = {
  nested: { deep: 1 },
}
;(root as any).x = 1
export * from './b.ts'
"""

SNIPPETS = [
    pytest.param(
        "tests/a.spec.ts",
        NESTED,
        {
            "a.spec.ts::A::B::x": "case",
            "a.spec.ts::A::y": "case",
            "tests/a.spec.ts:<rest>": "rest",
        },
        id="nested-describe",
    ),
    pytest.param(
        "tests/a.spec.ts",
        EACH,
        {
            "a.spec.ts::A::does p": "case",
            "a.spec.ts::A::does q": "case",
            "a.spec.ts::A::flag (false)": "case",
            "a.spec.ts::A::flag (true)": "case",
            "tests/a.spec.ts:<rest>": "rest",
        },
        id="it-each-rows",
    ),
    pytest.param(
        "tests/a.spec.ts",
        BRACES,
        {
            "a.spec.ts::A::braces": "case",
            "tests/a.spec.ts:after": "symbol",
            "tests/a.spec.ts:<rest>": "rest",
        },
        id="brace-in-string-template-and-regex",
    ),
    pytest.param(
        "src/a.ts",
        CLASS,
        {
            "src/a.ts:Foo": "symbol",
            "src/a.ts:Foo.size": "member",
            "src/a.ts:Foo.foo": "member",
            "src/a.ts:Foo.[symbols.root]": "member",
            "src/a.ts:Foo.constructor": "member",
        },
        id="class-members-overloads-computed-static-merge",
    ),
    pytest.param(
        "src/a.ts",
        DECLARATIONS,
        {
            "src/a.ts:Mode": "symbol",
            "src/a.ts:Options": "symbol",
            "src/a.ts:module ./context.ts": "symbol",
            "src/a.ts:State": "symbol",
            "src/a.ts:gen": "symbol",
            "src/a.ts:load": "symbol",
            "src/a.ts:value": "symbol",
            "src/a.ts:<rest>": "rest",
        },
        id="declarations-and-rest",
    ),
    pytest.param(
        "tests/a.spec.ts",
        "describe(`T`, function () {\n  it.each([-1, 2])('n %s', () => {})\n})\n",
        {
            "a.spec.ts::T::n -1": "case",
            "a.spec.ts::T::n 2": "case",
            "tests/a.spec.ts:<rest>": "rest",
        },
        id="template-title-function-callback-negative-row",
    ),
    pytest.param(
        "tests/a.spec.ts",
        "describe('T', () => {\n"
        "  it.each([undefined, null, -1, ['a', 1, -2], []])('r (%s)', () => {})\n"
        "  it.each([{ code: 'E1', n: 2 }])('o $code $n', () => {})\n"
        "  it.runIf(process.platform === 'win32')('win', () => {})\n"
        "})\n",
        {
            "a.spec.ts::T::r (undefined)": "case",
            "a.spec.ts::T::r (null)": "case",
            "a.spec.ts::T::r (-1)": "case",
            "a.spec.ts::T::r ([ 'a', 1, -2 ])": "case",
            "a.spec.ts::T::r ([])": "case",
            "a.spec.ts::T::o E1 2": "case",
            "a.spec.ts::T::win": "case",
            "tests/a.spec.ts:<rest>": "rest",
        },
        id="it-each-undefined-array-object-rows-and-run-if",
    ),
    pytest.param(
        "src/a.ts",
        "export class Foo {}\nexport default Foo\n",
        {"src/a.ts:Foo": "symbol", "src/a.ts:default": "symbol"},
        id="default-export-of-a-name",
    ),
]


def _scan(path: str, text: str) -> list:
    scan = inventory.scan_spec if path.startswith("tests/") else inventory.scan_source
    return scan(path, text)


@pytest.mark.parametrize(("path", "text", "expected"), SNIPPETS)
def test_scanner_gives_expected_items_and_partitions_tokens(
    path: str, text: str, expected: dict[str, str]
) -> None:
    items = _scan(path, text)

    assert {item.id: item.kind for item in items} == expected
    # Every token is in exactly one range; the rows of one `it.each` share a range.
    ranges = {span for item in items for span in item.spans}
    coverage = Counter(
        sum(start <= token.start and token.end <= end for start, end in ranges)
        for token in inventory.tokenize(text)
    )
    assert set(coverage) == {1}


def _hashes(text: str, path: str = "src/a.ts") -> dict[str, str]:
    return {item.id: inventory.item_hash(item, text) for item in _scan(path, text)}


def test_symbol_hash_excludes_member_text() -> None:
    before = _hashes(CLASS)
    after = _hashes(CLASS.replace("get size() { return 1 }", "get size() { return 2 }"))

    changed = {id_ for id_ in before if before[id_] != after[id_]}

    assert changed == {"src/a.ts:Foo.size"}


def test_overloads_and_merged_declarations_are_one_item() -> None:
    ids = [item.id for item in _scan("src/a.ts", CLASS)]

    assert ids.count("src/a.ts:Foo.foo") == 1
    assert ids.count("src/a.ts:Foo") == 1
    merged = next(
        item for item in _scan("src/a.ts", CLASS) if item.id == "src/a.ts:Foo"
    )
    assert len(merged.spans) > 1


@pytest.mark.parametrize(
    "table",
    [
        "rows",
        "[...rows]",
        "[1, mode]",
        "[[[1]]]",
        "[[1,, 2]]",
        "[{ a: [1] }]",
        "[{ a }]",
        "[{ , a: 1 }]",
        "[]",
    ],
)
def test_scanner_rejects_it_each_table_that_is_not_a_literal(table: str) -> None:
    text = f"it.each({table})('does %s', () => {{}})"

    with pytest.raises(ValueError, match=r"tests/a\.spec\.ts: offset \d+"):
        inventory.scan_spec("tests/a.spec.ts", text)


@pytest.mark.parametrize(
    "text",
    [
        "it.each(['a'])('no placeholder', () => {})",
        "it.each(['a'])('%d', () => {})",
        "it.each(['a'])('%s %s', () => {})",
        "it.runIf(ok)",
        "it.runIf",
        "it.each",
        "it.runIf(ok)(title, () => {})",
        "it.each([{ a: 1 }])('$b', () => {})",
        "it.each([{ a: 1 }])('%s', () => {})",
        "it.each([{ a: 1 }])('no key', () => {})",
        "it.skip('x', () => {})",
        "it(name, () => {})",
        "describe('A', () => { it('x', () => {}); it('x', () => {}) })",
        "for (const x of xs) { it('x', () => {}) }",
    ],
)
def test_scanner_rejects_constructs_it_cannot_place(text: str) -> None:
    with pytest.raises(ValueError, match=r"tests/a\.spec\.ts: offset \d+"):
        inventory.scan_spec("tests/a.spec.ts", text)


def test_hash_ignores_format_and_comments_and_sees_token_changes() -> None:
    base = "export function f(a: number) { return a + 1 }\n"
    reformatted = (
        "// note\nexport function f( a: number )\n{\n  /* body */ return a+1\n}\n"
    )
    changed = "export function f(a: number) { return a + 2 }\n"

    assert _hashes(base) == {id_: hash_ for id_, hash_ in _hashes(reformatted).items()}
    assert _hashes(base) != _hashes(changed)


def test_normalize_keeps_comment_markers_inside_strings() -> None:
    assert inventory.normalize("a('//x', /* c */ `/*y*/`)") == "a ( '//x' , `/*y*/` )"


def test_digest_is_sha256_of_text() -> None:
    assert inventory.digest("a") == (
        "ca978112ca1bbdcafac231b39a23dc4da786eff8147c4e72b9807785afee48bb"
    )


VOCABULARY = {
    "src/a.ts": """\
export class CordisError extends Error {
  constructor(public code: CordisError.Code, message?: string) {
    super(message ?? CordisError.Code[code])
  }
}
export namespace CordisError {
  export type Code = keyof typeof Code
  export const Code = {
    INACTIVE: 'cannot create effect on inactive context',
  } as const
}
export const enum FiberState { PENDING, LOADING = 1 }
export interface Events {
  [key: symbol]: (...args: any[]) => any
  'internal/plugin'(fiber: Fiber): void
  'internal/status'(fiber: Fiber, old: FiberState): void
  'other/name'(): void
}
function f(name: string, kind: unknown) {
  if (!name) throw new Error(`service "${name}" has been registered at <${name.x}>`)
  if (!kind) throw new TypeError('invalid component, received ' + typeof kind)
  throw new Error(name)
}
function g() {
  throw new AggregateError(errors, 'one or more of ' + label + ' failed')
}
""",
}


def test_vocabulary_gives_messages_events_and_states() -> None:
    items = {item.id: item for item in inventory.vocabulary(VOCABULARY)}

    assert {id_: item.text for id_, item in items.items()} == {
        "message:cannot create effect on inactive context": (
            "cannot create effect on inactive context"
        ),
        'message:service "{}" has been registered at <{}>': (
            'service "{}" has been registered at <{}>'
        ),
        "message:invalid component, received {}": "invalid component, received {}",
        "message:one or more of {} failed": "one or more of {} failed",
        "event:internal/plugin": "internal/plugin",
        "event:internal/status": "internal/status",
        "state:PENDING": "PENDING",
        "state:LOADING": "LOADING",
    }
    assert {item.kind for item in items.values()} == {"message", "event", "state"}


@pytest.mark.parametrize(
    "text",
    ["a = 'open", "a = `open", "a = `${ 1", "/* open", "a = /open", "f(", "f)", "f(]"],
)
def test_scanner_rejects_text_it_cannot_read(text: str) -> None:
    with pytest.raises(ValueError, match=r"src/a\.ts: offset \d+"):
        inventory.scan_source("src/a.ts", text)


VOCABULARY_KINDS = ("message:", "state:", "event:")


def test_inventory_lists_hashes_and_vocabulary_but_no_code(tmp_path: Path) -> None:
    core = tmp_path / "packages" / "core"
    (core / "src").mkdir(parents=True)
    (core / "tests").mkdir()
    (core / "src" / "a.ts").write_text(
        "export function f() { throw new Error('secret body text') }\n"
        "export const enum FiberState { PENDING }\n",
        encoding="utf-8",
    )
    (core / "tests" / "a.spec.ts").write_text(
        "describe('A', () => { it('x', () => {}) })\n", encoding="utf-8"
    )

    result = inventory.build_inventory(tmp_path, "abc123")

    assert result["revision"] == "abc123"
    items = result["items"]
    assert list(items) == sorted(items)
    assert items["a.spec.ts::A::x"]["kind"] == "case"
    assert items["src/a.ts:f"]["kind"] == "symbol"
    assert items["state:PENDING"] == {
        "kind": "state",
        "hash": inventory.digest("PENDING"),
        "text": "PENDING",
    }
    assert items["message:secret body text"]["text"] == "secret body text"
    code_items = [e for id_, e in items.items() if not id_.startswith(VOCABULARY_KINDS)]
    assert code_items
    assert all(set(entry) == {"kind", "hash"} for entry in code_items)


def test_ledger_stubs_are_pending_entries_for_missing_items_only() -> None:
    tricky = 'message:a "quoted" \\ back\nslash'
    items = {"a.ts:A": {}, "b.ts:B": {}, tricky: {}}
    stubs = tomllib.loads(inventory.ledger_stubs(items, {"a.ts:A"}))
    assert stubs == {
        "items": {"b.ts:B": {"status": "pending"}, tricky: {"status": "pending"}}
    }


def test_package_items_take_the_package_directory_as_prefix(tmp_path: Path) -> None:
    core = tmp_path / "packages" / "core"
    pkg = tmp_path / "packages" / "loader"
    for root in (core, pkg):
        (root / "tests").mkdir(parents=True)
    (core / "src").mkdir()
    (pkg / "src" / "config").mkdir(parents=True)
    (core / "src" / "a.ts").write_text("export const x = 1\n", encoding="utf-8")
    (core / "tests" / "a.spec.ts").write_text(
        "it('same name', () => {})\n", encoding="utf-8"
    )
    (pkg / "src" / "config" / "a.ts").write_text(
        "export function f() { throw new Error('pkg message') }\n", encoding="utf-8"
    )
    (pkg / "tests" / "a.spec.ts").write_text(
        "it('same name', () => {})\n", encoding="utf-8"
    )

    items = inventory.build_inventory(tmp_path, "abc", ["loader"])["items"]

    assert set(items) == {
        "src/a.ts:x",
        "a.spec.ts::same name",
        "loader/src/config/a.ts:f",
        "loader/a.spec.ts::same name",
        "loader/message:pkg message",
    }


def test_main_reads_the_package_list_from_the_ledger(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    for package in ("core", "loader"):
        (clone / "packages" / package / "src").mkdir(parents=True)
        (clone / "packages" / package / "tests").mkdir()
    (clone / "packages" / "loader" / "src" / "a.ts").write_text(
        "export const x = 1\n", encoding="utf-8"
    )
    git = ["git", "-C", str(clone), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    ledger = tmp_path / "ledger.toml"
    ledger.write_text('[inventory]\npackages = ["loader"]\n', encoding="utf-8")
    out = tmp_path / "upstream.json"

    status = inventory.main([str(clone), "--out", str(out), "--ledger", str(ledger)])

    assert status == 0
    assert list(json.loads(out.read_text("utf-8"))["items"]) == ["loader/src/a.ts:x"]
