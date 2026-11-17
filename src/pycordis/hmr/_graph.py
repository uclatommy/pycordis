"""The module graph: which loaded modules a module imports."""

import ast
import importlib.machinery
import logging
import os
import sys
import sysconfig
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

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


def loaded_module(name: str) -> ModuleType | None:
    """Return the module `sys.modules` holds as `name`, or `None`.

    `sys.modules` can hold any object, such as a class that stands for a deprecated
    module. A value that is not a module gives `None`, as a missing name does.
    """
    value = sys.modules.get(name)
    return value if isinstance(value, ModuleType) else None


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
    parent = loaded_module(name.rpartition(".")[0])
    path = parent.__dict__.get("__path__") if parent is not None else None
    return (
        path is not None
        and importlib.machinery.PathFinder.find_spec(name, list(path)) is not None
    )


_UNREADABLE: Any = object()
"""The result of `EdgeCache._read` for a file that cannot be read."""


class EdgeCache:
    """The imports of each loaded module, as they were when the module was first seen.

    A file that changed holds the new source, so the old imports of a loaded module
    cannot come from its file. The cache reads the edges of a module once, with the
    graph function, and keeps them for as long as the same module object is loaded.
    A module object that replaced the cached one (an import after eviction) has its
    edges read again, and a restored module object keeps the edges that it had. A
    `sys.modules` value that is not a module is outside the graph.
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
        known, edges = self._scan({})
        self._known = known
        return edges

    def peek(
        self, unloaded: Mapping[str, ModuleType] | None = None
    ) -> dict[str, tuple[str, ...]]:
        """Return the edges that `snapshot` gives, and leave the cache as it is.

        Args:
            unloaded: Modules that `sys.modules` does not hold, by name, such as the old
                module objects of a failed import. The graph function reads each one
                again, so a file that changed gives its new edges.
        """
        return self._scan(unloaded or {})[1]

    def _scan(
        self, unloaded: Mapping[str, ModuleType]
    ) -> tuple[
        dict[str, tuple[ModuleType, tuple[str, ...] | None]],
        dict[str, tuple[str, ...]],
    ]:
        """Return the new cache content and the edges of the modules in the graph."""
        known: dict[str, tuple[ModuleType, tuple[str, ...] | None]] = {}
        unreadable: set[str] = set()
        loaded = dict(sys.modules)
        for name, module in loaded.items():
            cached = self._known.get(name)
            if cached is not None and cached[0] is module:
                known[name] = cached
                continue
            edges = self._read(name, module)
            if edges is _UNREADABLE:
                # not cached, so that the next snapshot reads the file again
                unreadable.add(name)
            else:
                known[name] = (module, edges)
        for name, module in unloaded.items():
            edges = self._read(name, module)
            if edges is _UNREADABLE:
                unreadable.add(name)
            else:
                known[name] = (module, edges)
        edges_of = {
            name: edges for name, (_, edges) in known.items() if edges is not None
        }
        edges_of.update({name: () for name in unreadable})
        return known, {
            name: tuple(child for child in edges if child in edges_of)
            for name, edges in edges_of.items()
        }

    def _read(self, name: str, module: object) -> tuple[str, ...] | None:
        """Return the edges of `module`, `None` outside the graph, or `_UNREADABLE`.

        A value that is not a module is outside the graph, so the graph function gets
        only modules.
        """
        if not isinstance(module, ModuleType):
            return None
        try:
            edges = self._graph(module)
        except (SyntaxError, OSError):
            logger.warning("cannot read the imports of %s", name, exc_info=True)
            return _UNREADABLE
        return None if edges is None else tuple(edges)


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
    changed: Iterable[str],
    edges: Mapping[str, Collection[str]],
    hosts: Collection[str] = (),
) -> set[str]:
    """Return the changed modules and every module that imports one of them.

    A module that imports an accepted module is accepted too, also when no component
    uses it, so that a later import of it gets the new code. A module in `hosts` never
    counts as an importer, because it never reloads.
    """
    importers: dict[str, list[str]] = {}
    for name, children in edges.items():
        if name in hosts:
            continue
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


@dataclass(slots=True, eq=False)
class Unit[T]:
    """Items that commit, abort, or skip together.

    Attributes:
        items: The items of the unit, in their input order.
        modules: The evicted modules that the items reach.
    """

    items: list[T] = field(default_factory=list)
    modules: set[str] = field(default_factory=set)


def partition[T](
    items: Sequence[T], reaches: Sequence[Collection[str]], evicted: Collection[str]
) -> list[Unit[T]]:
    """Group the items whose reach shares an evicted module.

    Two items are related when their reaches share an evicted module, and a unit is a
    set of items that are related directly or through other items. Units come in the
    order of their first item.

    Args:
        items: The items to group.
        reaches: The modules that each item reaches, in the old and the new graph.
        evicted: The modules that eviction removed. Other modules do not relate items.
    """
    parents = list(range(len(items)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owners: dict[str, int] = {}
    for index, reach in enumerate(reaches):
        for name in reach:
            if name in evicted:
                owner = owners.setdefault(name, index)
                parents[find(index)] = find(owner)
    units: dict[int, Unit[T]] = {}
    for index, item in enumerate(items):
        unit = units.setdefault(find(index), Unit())
        unit.items.append(item)
        unit.modules.update(name for name in reaches[index] if name in evicted)
    return list(units.values())
