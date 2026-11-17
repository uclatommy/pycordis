"""The module table: match files, evict and restore modules, find components."""

import os
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from pycordis.hmr._graph import module_file
from pycordis.registry import Component, RegistryService, Runtime


def match_roots(roots: set[str], names: Iterable[str]) -> set[str]:
    """Return the names of the loaded modules whose files are a root or lie below one.

    The ancestors of each file are looked up in `roots`, so a root matches whole path
    segments only: `/a/root` does not match `/a/root-sibling/m.py`.
    """
    matched: set[str] = set()
    for name in names:
        file = module_file(sys.modules[name])
        if file is None:
            continue
        path = os.path.normpath(os.path.abspath(file))
        while True:
            if path in roots:
                matched.add(name)
                break
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    return matched


@dataclass(frozen=True, slots=True)
class _Saved:
    """A module that eviction removed, with the attribute of its parent package."""

    module: ModuleType
    parent: ModuleType | None
    attribute: Any


class Backup:
    """The modules that `evict` removed, kept so that `restore` can put them back."""

    def __init__(self) -> None:
        self._saved: dict[str, _Saved] = {}

    def evict(self, names: Iterable[str]) -> None:
        """Remove the loaded modules `names` from `sys.modules`, and from their parents.

        `import a.b` sets the attribute `b` on the package `a`. Eviction removes the
        attribute too, so that a later `import a.b` binds the new module.
        """
        # a submodule goes before its package, so that it still finds the package
        for name in sorted(names, key=lambda name: -name.count(".")):
            module = sys.modules.pop(name)
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name) if parent_name else None
            attribute = None
            if parent is not None and child in parent.__dict__:
                attribute = parent.__dict__.pop(child)
            self._saved[name] = _Saved(module, parent, attribute)

    def restore(self, names: Iterable[str]) -> None:
        """Put the evicted modules `names` back, over a module that took the name."""
        for name in names:
            saved = self._saved[name]
            sys.modules[name] = saved.module
            if saved.parent is not None and saved.attribute is not None:
                setattr(saved.parent, name.rpartition(".")[2], saved.attribute)


@dataclass(frozen=True, slots=True)
class Exporter:
    """A place where a module holds a component.

    Attributes:
        module: The module name.
        attribute: The attribute of the module that holds the component, or `None` when
            the module is the component.
        value: The component at the time of the search.
    """

    module: str
    attribute: str | None
    value: Component

    def read(self, module: ModuleType) -> object:
        """Return the value that `module` (the new version) holds in this place."""
        if self.attribute is None:
            return module
        return getattr(module, self.attribute, None)


def find_components(
    names: Iterable[str], registry: RegistryService
) -> dict[Runtime, list[Exporter]]:
    """Group the components that the modules `names` export by their runtime record.

    A component of a module is the module itself, or a value in its namespace, that
    `registry` has a live fiber for. A module that several places export, such as an
    entry module that re-exports an implementation, is one group.
    """
    groups: dict[Runtime, list[Exporter]] = {}
    for name in names:
        module = sys.modules[name]
        places = [(None, module)] + [
            (attribute, value)
            for attribute, value in list(module.__dict__.items())
            if not attribute.startswith("__")
        ]
        for attribute, value in places:
            try:
                runtime = registry.get(value)
            except TypeError:
                continue  # a callable value that cannot be hashed is no component
            if runtime is not None:
                groups.setdefault(runtime, []).append(Exporter(name, attribute, value))
    return groups
