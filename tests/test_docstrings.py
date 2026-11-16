"""Docstring guard for the pycordis package.

A docstring in ``src/pycordis`` explains pycordis to a Python developer who has not
read the reference implementation, so it never names upstream, upstream's languages,
or their constructs. The guard scans module, class, function, and async-function
docstrings with ``ast`` and rejects the banned terms below. It does not read ``#``
comments, which are the place for maintainer notes about upstream.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "pycordis"

_BANNED_TERMS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("upstream", re.compile(r"\bupstream\b", re.IGNORECASE)),
    ("TypeScript", re.compile(r"\bTypeScript\b", re.IGNORECASE)),
    ("JavaScript", re.compile(r"\bJavaScript\b", re.IGNORECASE)),
    ("Promise", re.compile(r"\bPromise\b")),
    ("Proxy", re.compile(r"\bProxy\b")),
    ("WeakMap", re.compile(r"\bWeakMap\b")),
    (".ts", re.compile(r"\.ts\b")),
)

_DEFINITION_NODES = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _definitions(node: ast.AST, prefix: str) -> Iterator[tuple[str, ast.AST]]:
    """Yield ``(qualified name, node)`` for every class or function below ``node``."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _DEFINITION_NODES):
            name = f"{prefix}.{child.name}" if prefix else child.name
            yield name, child
            yield from _definitions(child, name)


def docstring_violations(source: str, location: str) -> list[str]:
    """Return one failure message per docstring in ``source`` that names a banned term.

    Args:
        source: The Python source code to scan.
        location: The file name to write into each failure message.

    Returns:
        One message per docstring and banned term, enclosing definition first.
    """
    tree = ast.parse(source)
    documents: list[tuple[str, str]] = [("module", ast.get_docstring(tree) or "")]
    for name, node in _definitions(tree, ""):
        doc = ast.get_docstring(node)
        if doc is not None:
            documents.append((name, doc))
    return [
        f"{location}: {label} docstring names {term}"
        for label, doc in documents
        for term, pattern in _BANNED_TERMS
        if pattern.search(doc)
    ]


_BANNED_SENTENCES = (
    "as upstream defines it",
    "the UPSTREAM revision",
    "a TypeScript host",
    "idiomatic JavaScript",
    "await the Promise",
    "a Proxy object",
    "a WeakMap cache",
    "copied from fiber.ts",
)

_ALLOWED_SENTENCES = (
    "a promise of determinism",
    "the proxy pattern",
    "weakmap is not a name",
    "a claim of upstreamness",
    "a .tsx build",
)


@pytest.mark.parametrize(
    ("sentence", "flagged"),
    [(s, True) for s in _BANNED_SENTENCES] + [(s, False) for s in _ALLOWED_SENTENCES],
)
def test_guard_flags_banned_terms_only_when_they_appear_as_words(
    sentence: str, flagged: bool
) -> None:
    source = f'"""Keep {sentence} in the module docstring."""\n'
    assert bool(docstring_violations(source, "sample.py")) is flagged


def test_guard_reads_module_class_function_and_async_function_docstrings() -> None:
    source = (
        '"""Reference upstream counts."""\n'
        "\n"
        "class Reader:\n"
        '    """Reference upstream rows."""\n'
        "\n"
        "    def read(self) -> int:\n"
        '        """Reference upstream values."""\n'
        "        return 1\n"
        "\n"
        "    class Inner:\n"
        '        """Reference upstream pages."""\n'
        "\n"
        "        def run(self) -> None:\n"
        '            """Reference upstream offsets."""\n'
        "\n"
        "async def load() -> None:\n"
        '    """Reference upstream data."""\n'
    )
    assert docstring_violations(source, "sample.py") == [
        "sample.py: module docstring names upstream",
        "sample.py: Reader docstring names upstream",
        "sample.py: Reader.read docstring names upstream",
        "sample.py: Reader.Inner docstring names upstream",
        "sample.py: Reader.Inner.run docstring names upstream",
        "sample.py: load docstring names upstream",
    ]


def test_guard_ignores_comments_that_name_banned_terms() -> None:
    source = "# upstream calls this a Promise\nVALUE = 1  # see fiber.ts on a Proxy\n"
    assert docstring_violations(source, "sample.py") == []


def _tree_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        violations.extend(docstring_violations(source, path.name))
    return violations


def test_src_docstrings_name_no_banned_terms() -> None:
    assert _tree_violations(SRC_ROOT) == []


def test_tree_scan_reaches_packages_in_subdirectories(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    clean = tmp_path / "pkg" / "__init__.py"
    clean.write_text('"""A clean docstring."""\n', encoding="utf-8")
    broken = tmp_path / "pkg" / "sub" / "inner.py"
    broken.write_text('"""Names upstream."""\n', encoding="utf-8")
    assert _tree_violations(tmp_path) == ["inner.py: module docstring names upstream"]
