"""The module graph: which loaded modules a module imports."""

import ast
import importlib.machinery
import logging
import os
import sys
import sysconfig
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

type ModuleGraph = Callable[[ModuleType], Collection[str] | None]
"""A function that gives the names of the modules that a loaded module imports.

It returns `None` for a module that is outside the graph. A module outside the graph
never reloads. A module inside the graph with no imports gives an empty collection.
"""

_SYSCONFIG_KEYS = ("stdlib", "platstdlib", "purelib", "platlib")


def module_file(module: ModuleType) -> str | None:
    """Return the file of `module`, or `None` when it has no file.

    A built-in module, a frozen module, and a namespace package have no file. The
    `__main__` module of a script has no import spec, and its file is `__file__`.
    """
    spec = module.__spec__
    if spec is None:
        return module.__dict__.get("__file__")
    return spec.origin if spec.has_location else None


def source_graph(scope: Iterable[str]) -> ModuleGraph:
    """Build the default `ModuleGraph`, which reads the imports of a file.

    The graph covers the loaded modules whose files are below a directory of `scope`
    and are not below a directory of the Python installation (the `stdlib`,
    `platstdlib`, `purelib`, and `platlib` paths of `sysconfig`). The exclusion is
    necessary because a virtual environment is often a directory inside the project. A
    module with no file is outside the graph.

    The edges of a module are the modules that its `import` and `from ... import`
    statements name, also inside functions and `if` blocks, and its parent package.
    `import a.b.c` and `from a.b import c` have an edge to `a`, `a.b`, and, when `c` is
    a module, `a.b.c`. A relative import resolves against the package of the module. A
    call to `importlib.import_module` is not an edge. A file that is not Python source
    (a C extension or a bytecode file) has no edges.

    Pass the result to `Hmr`, or wrap it to change the graph.

    Args:
        scope: The directories that hold the modules of the graph.

    Returns:
        A function from a loaded module to the names of the modules that it imports,
        or `None` when the module is outside the graph. It raises `SyntaxError` when
        the source file of a module in the graph is not valid Python.

    Examples:
        ```python
        >>> graph = source_graph([os.getcwd()])
        >>> import json
        >>> graph(json) is None  # the standard library is outside the graph
        True
        ```
    """
    roots = tuple(os.path.abspath(directory) for directory in scope)
    excluded = tuple(
        os.path.abspath(path)
        for key in _SYSCONFIG_KEYS
        if (path := sysconfig.get_paths().get(key))
    )

    def graph(module: ModuleType) -> Collection[str] | None:
        file = module_file(module)
        if file is None:
            return None
        path = Path(os.path.abspath(file))
        if not any(path.is_relative_to(root) for root in roots):
            return None
        if any(path.is_relative_to(directory) for directory in excluded):
            return None
        names = _import_names(module, path) if path.suffix == ".py" else []
        parent = module.__name__.rpartition(".")[0]
        if parent:
            names.append(parent)
        return tuple(dict.fromkeys(name for name in names if name != module.__name__))

    return graph


def _import_names(module: ModuleType, path: Path) -> list[str]:
    """Return the module names that the import statements in `path` name."""
    tree = ast.parse(path.read_bytes(), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.extend(_prefixes(alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_base(module, node)
            if base is None:
                continue  # an import that goes above the top package fails when it runs
            names.extend(_prefixes(base) if base else [])
            names.extend(
                f"{base}.{alias.name}"
                for alias in node.names
                if alias.name != "*" and _is_module(f"{base}.{alias.name}")
            )
    return names


def _absolute_base(module: ModuleType, node: ast.ImportFrom) -> str | None:
    """Return the absolute name that `from <base> import ...` of `node` means."""
    if node.level == 0:
        return node.module or ""
    package = (module.__package__ or "").split(".")
    keep = len(package) - (node.level - 1)
    if not module.__package__ or keep < 1:
        return None
    return ".".join([*package[:keep], *([node.module] if node.module else [])])


def _prefixes(name: str) -> list[str]:
    """Return `a`, `a.b`, and `a.b.c` for `a.b.c`."""
    parts = name.split(".")
    return [".".join(parts[: i + 1]) for i in range(len(parts))]


def _is_module(name: str) -> bool:
    """Tell whether `name` is a module, without importing it."""
    if name in sys.modules:
        return True
    parent = sys.modules.get(name.rpartition(".")[0])
    path = parent.__dict__.get("__path__") if parent is not None else None
    return (
        path is not None
        and importlib.machinery.PathFinder.find_spec(name, list(path)) is not None
    )


class EdgeCache:
    """The imports of each loaded module, as they were when the module was first seen.

    A file that changed holds the new source, so the old imports of a loaded module
    cannot come from its file. The cache reads the edges of a module once, with the
    graph function, and keeps them for as long as the same module object is loaded.
    A module object that replaced the cached one (an import after eviction) has its
    edges read again, and a restored module object keeps the edges that it had.
    """

    def __init__(self, graph: ModuleGraph) -> None:
        self._graph = graph
        self._known: dict[str, tuple[ModuleType, tuple[str, ...] | None]] = {}

    def snapshot(self) -> dict[str, tuple[str, ...]]:
        """Return the edges of every loaded module in the graph, and update the cache.

        An edge to a module that is not loaded and in the graph is dropped, so a name
        in the result is a key of the result. A module that is no longer loaded leaves
        the cache.
        """
        known: dict[str, tuple[ModuleType, tuple[str, ...] | None]] = {}
        unreadable: set[str] = set()
        for name, module in dict(sys.modules).items():
            cached = self._known.get(name)
            if cached is not None and cached[0] is module:
                known[name] = cached
                continue
            try:
                edges = self._graph(module)
            except (SyntaxError, OSError):
                # not cached, so that the next snapshot reads the file again
                logger.warning("cannot read the imports of %s", name, exc_info=True)
                unreadable.add(name)
                continue
            known[name] = (module, None if edges is None else tuple(edges))
        self._known = known
        edges_of = {
            name: edges for name, (_, edges) in known.items() if edges is not None
        }
        edges_of.update({name: () for name in unreadable})
        return {
            name: tuple(child for child in edges if child in edges_of)
            for name, edges in edges_of.items()
        }


def closure(
    roots: Iterable[str],
    edges: Mapping[str, Collection[str]],
    stop: Callable[[str], bool] = lambda name: False,
) -> set[str]:
    """Return the modules that `roots` reach along `edges`, and the roots.

    The traversal neither includes nor passes through a module for which `stop` holds.
    A name that is not a key of `edges` is not included.
    """
    reached: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in reached or name not in edges or stop(name):
            continue
        reached.add(name)
        pending.extend(edges[name])
    return reached


def accepted_modules(
    changed: Iterable[str], edges: Mapping[str, Collection[str]]
) -> set[str]:
    """Return the changed modules and every module that imports one of them.

    A module that imports an accepted module is accepted too, also when no component
    uses it, so that a later import of it gets the new code.
    """
    importers: dict[str, list[str]] = {}
    for name, children in edges.items():
        for child in children:
            importers.setdefault(child, []).append(name)
    accepted: set[str] = set()
    pending = list(changed)
    while pending:
        name = pending.pop()
        if name in accepted:
            continue
        accepted.add(name)
        pending.extend(importers.get(name, ()))
    return accepted
