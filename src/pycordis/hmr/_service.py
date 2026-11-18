"""The `Hmr` service: a transaction that reloads modules and moves component fibers."""

import contextlib
import importlib
import importlib.machinery
import json
import os
import sys
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import Fiber
from pycordis.hmr._graph import (
    EdgeCache,
    ModuleGraph,
    Unit,
    accepted_modules,
    closure,
    loaded_module,
    module_file,
    partition,
    source_graph,
)
from pycordis.hmr._modules import (
    Backup,
    Exporter,
    find_components,
    match_roots,
)
from pycordis.logger import Logger
from pycordis.registry import Component, RegistryService
from pycordis.scheduler import owner_thread, start
from pycordis.service import Service

type Changes = Callable[
    [], Iterable[str | os.PathLike[str]] | Deferred[Iterable[str | os.PathLike[str]]]
]


class SkipReason(StrEnum):
    """Why a change cannot apply without a restart.

    Attributes:
        HOST_MODULE: The module is one of the host modules (see `Hmr.host_modules`).
            The host holds its objects, so it never reloads.
        EXTENSION_MODULE: The module is a C extension. A process cannot load an
            extension module again.
        AMBIGUOUS_EXPORT: The modules that exported one component now export
            different values for it, and the fibers of the component do not record
            which module each one came from.
    """

    HOST_MODULE = "host-module"
    EXTENSION_MODULE = "extension-module"
    AMBIGUOUS_EXPORT = "ambiguous-export"


@dataclass(frozen=True, slots=True)
class Abort:
    """A unit of modules whose new version failed to load, so the old version stays.

    Nothing in the unit applies. The old modules are in `sys.modules` again, and a later
    change of the files retries the unit.

    Attributes:
        modules: The names of the modules of the unit that are restored.
        error: The first error that the import of the unit raised.
    """

    modules: list[str]
    error: BaseException


@dataclass(frozen=True, slots=True)
class Skip:
    """A change that cannot apply without a restart. The other changes apply.

    A skipped unit keeps its old modules and its fibers, as an `Abort` does.

    Attributes:
        modules: The name of the changed host or extension module, or the names of
            the modules of the skipped unit.
        reason: Why the change cannot apply.
    """

    modules: list[str]
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """The outcome of one `Hmr.reconcile` transaction.

    Attributes:
        settled: The fibers that moved to new code and still exist after they settled.
            A fiber whose new component failed to apply is in the list, in the state
            `FAILED`.
        aborted: The units whose new modules failed to import or did not export a
            component.
        skipped: The changes that need a restart.
    """

    settled: list[Fiber] = field(default_factory=list)
    aborted: list[Abort] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class HmrConfig:
    """The config of `Hmr`.

    Attributes:
        scope: The directories whose modules can reload. An empty tuple means the
            working directory at the start of the service.
        host_modules: The names of modules that never reload, in addition to the
            modules that the `__main__` module imports.
    """

    scope: tuple[str, ...] = ()
    host_modules: frozenset[str] = frozenset()


@dataclass(slots=True, eq=False)
class _Stale:
    """A registered component that a changed module exports, and its import attempt."""

    entry: Exporter
    """The place that the replacement comes from."""
    entries: list[str]
    """The modules that no other module of the component imports, `entry` first."""
    exporters: list[Exporter]
    """Every place that holds the component."""
    modules: set[str]
    """The closure of the component in the old module graph."""
    replacement: Component | None = None
    error: BaseException | None = None
    skip: SkipReason | None = None
    """Why the component cannot be replaced in place, when it cannot."""
    loaded: list[str] = field(default_factory=list)
    """The modules that the attempt loaded for the first time."""

    def roots(self) -> list[str]:
        """Return the modules whose new imports decide the unit of the component."""
        return [
            *dict.fromkeys(exporter.module for exporter in self.exporters),
            *self.entries,
        ]


@dataclass(slots=True, eq=False)
class _Running:
    """The transaction that runs now."""

    inside: bool = False
    """Whether the transaction runs synchronous code now."""
    moved: set[Fiber] = field(default_factory=set)
    """The fibers that stage 2 replaced."""

    @contextlib.contextmanager
    def synchronous(self) -> Iterator[None]:
        """Mark the code in the block as a part of the transaction."""
        self.inside = True
        try:
            yield
        finally:
            self.inside = False


class Hmr(Service):
    """The service that reloads changed modules and moves component fibers.

    A development tool or an interactive host that knows which files changed calls
    `reconcile`. The service finds the loaded modules that the change affects, imports
    them again, and moves the live fibers of the components that they export to the new
    code with `RegistryService.replace`. A fiber keeps its identity, its config, and its
    place in the fiber tree.

    A transaction runs in three stages. It imports the new modules and checks each new
    component. Components that share a changed module form one unit, and a unit
    commits, aborts, or is skipped as a whole. If an import of a unit fails, or a new
    value is not a component, the unit is aborted: its old modules come back and
    nothing in it applies. A unit that cannot apply in place is skipped in the same
    way. The other units replace their components, and then the transaction waits for
    the moved fibers. A new component whose `apply` raises leaves its fiber `FAILED`,
    and the next change of its file retries it.

    The service sees the modules that its graph function covers (by default the
    modules below `HmrConfig.scope`, outside the Python installation). A module outside
    the graph never reloads.

    Only one `Hmr` is active in a process, because `sys.modules` belongs to the
    process. A second one fails at start with `CordisError`, and can start after the
    first is disposed.

    !!! warning "Host limits"

        - An object that the host holds, and a name that a host module imported with
          `from plugin import name`, keep the old code.
        - The module globals of a reloaded module start again from the module source.
          A C extension does not reload.
        - A reload affects the whole process: other root contexts and other libraries
          that imported a reloaded module also see the new module.
        - Python checks a bytecode cache file against the modification time of the
          source in whole seconds, and against its size. A change in the same second
          that keeps the size can load the old bytecode.
        - The old imports of a module that was imported after the last transaction, and
          that changed before the next one, come from its new source.
        - `reconcile` finds a nested call only through a view of the service, which
          `ctx["hmr"]` returns. A component that calls `reconcile` reads it this way,
          so the component, or an ancestor, must inject `hmr`. A call through a
          reference that the host keeps outside a context is not found. If a moved
          fiber waits for such a call, the transaction does not end.

    Call every method on the owner thread.

    Args:
        ctx: The context of the component.
        config: The `HmrConfig`, or `None` for the default.
        graph: The `ModuleGraph` function that gives the imports of a module. The
            default is `source_graph` over `HmrConfig.scope`.

    Raises:
        CordisError: At start, if another `Hmr` is active in the process.

    Examples:
        ```python
        >>> fiber = root.use(Hmr)
        >>> result = root.get("hmr").reconcile(lambda: ["/app/plugins/greet.py"])
        >>> result.done()
        True
        ```

    Attributes:
        config: The `HmrConfig`.
        host_modules: The names of the modules that never reload: the modules that the
            `__main__` module imports, found at start, and `HmrConfig.host_modules`.
    """

    provide = "hmr"
    _active: ClassVar[bool] = False

    def __init__(
        self,
        ctx: Context,
        config: HmrConfig | None = None,
        *,
        graph: ModuleGraph | None = None,
    ) -> None:
        super().__init__(ctx)
        self.config = config or HmrConfig()
        self.host_modules: frozenset[str] = self.config.host_modules
        scope = self.config.scope or (os.getcwd(),)
        self._edges = EdgeCache(graph or source_graph(scope))
        self._tail: Deferred[None] | None = None
        self._running: _Running | None = None

    @property
    def _logger(self) -> Logger:
        return self.ctx.logger("hmr")

    def cordis_init(self) -> Callable[[], None]:
        """Find the host modules, and take the one active place of the process."""
        if Hmr._active:
            raise CordisError("another HMR instance is active in this process")
        edges = self._edges.snapshot()
        main = loaded_module("__main__")
        if main is not None and module_file(main) is not None:
            self.host_modules |= closure(["__main__"], edges)
        Hmr._active = True
        return _release

    @owner_thread("ctx.scheduler")
    def reconcile(self, changes: Changes) -> Deferred[ReloadResult]:
        """Reload the modules that the changed paths affect, and move their fibers.

        `changes` runs inside the transaction and returns the changed paths: absolute
        paths of files or directories. A loaded module is changed when its file is a
        path or is below a path. Every module that imports a changed module reloads too,
        also when no component uses it, so that a later import gets the new code.
        A registered component that a reloaded module exports moves to the value of the
        same attribute in the new module. A component that an unchanged module exports
        and a reloaded module imports by name keeps its fibers.

        A host module or a C extension module that changed is skipped, and the other
        changes apply.

        Transactions run one at a time: a call waits for the running transaction.
        A call from inside the running transaction rejects at once: a call that runs
        while the transaction runs synchronous code (`changes`, an import, or the start
        of a moved fiber), and a call from a fiber that the transaction moved, or from
        a fiber below one. A call from a moved fiber after the transaction ended runs
        normally.
        If `changes` raises, or returns a `Deferred` that rejects, the transaction
        rejects with that error and `sys.modules` is as it was. The same holds for any
        other error that ends the transaction after the modules are evicted, such as an
        error of the host `graph` function or a `KeyboardInterrupt` from an import: the
        transaction restores every evicted module, removes the modules that it loaded
        for the first time, and rejects with that error. An `Exception` from the import
        of a component is not such an error: it aborts that component only.

        The import of a module runs on the owner thread and blocks it until the import
        ends.

        Args:
            changes: A function that returns the changed paths, or a `Deferred` of them.

        Returns:
            A `Deferred` that resolves with the `ReloadResult` after the moved fibers
            settle. An error from an import, from `RegistryService.replace`, or from a
            moved fiber goes to the log and does not reject it. It rejects with
            `CordisError("cannot start a transaction inside another transaction")` for
            a nested call, and with `TypeError` for a path that is not absolute.

        Examples:
            ```python
            >>> hmr = root.get("hmr")
            >>> result = hmr.reconcile(lambda: ["/app/plugins/greet.py"]).result()
            >>> [fiber.uid is not None for fiber in result.settled]
            [True]
            ```
        """
        if self._is_nested(self.ctx.fiber):
            nested = Deferred[ReloadResult]()
            nested.reject(
                CordisError("cannot start a transaction inside another transaction")
            )
            return nested
        previous = self._tail
        done = Deferred[None]()
        self._tail = done
        with self.ctx.scheduler.call():
            return start(
                self.ctx.scheduler,
                self._transaction(previous, done, changes),
                fiber=self.ctx.fiber,
            )

    def _is_nested(self, caller: Fiber) -> bool:
        """Tell whether a call from `caller` runs inside the running transaction."""
        running = self._running
        if running is None:
            return False
        if running.inside:
            return True
        fiber = caller
        while fiber not in running.moved:
            parent = fiber.parent.fiber
            if parent is fiber:
                return False
            fiber = parent
        return True

    async def _transaction(
        self, previous: Deferred[None] | None, done: Deferred[None], changes: Changes
    ) -> ReloadResult:
        try:
            if previous is not None:
                await previous
            running = self._running = _Running()
            with running.synchronous():
                produced = changes()
            if isinstance(produced, Deferred):
                produced = await produced
            roots = _roots(produced)
            result = ReloadResult()
            with running.synchronous():
                fibers = self._apply(roots, result)
            running.moved.update(fibers)
            for fiber in fibers:
                # the fiber has logged its own failure
                with contextlib.suppress(Exception):
                    await fiber.wait()
            result.settled.extend(fiber for fiber in fibers if fiber.uid is not None)
            return result
        finally:
            self._running = None
            done.resolve(None)

    def _apply(self, roots: set[str], result: ReloadResult) -> list[Fiber]:
        """Run stages 1 and 2, record the skips and aborts, and return the moved fibers.

        The method changes only `sys.modules` and the registry, so it has no `await`.
        """
        old = self._edges.snapshot()
        changed = self._classify(match_roots(roots, old), result)
        if not changed:
            return []
        accepted = accepted_modules(changed, old, self.host_modules)
        stale = self._stale_components(accepted, old)
        owned = set().union(*(item.modules for item in stale))
        for name in sorted(changed - owned):
            self._logger.debug("evict %s (no dependent component)", name)

        backup = Backup()
        present = set(sys.modules)
        # an error that ends the transaction here puts every module back, so that the
        # transaction commits or leaves `sys.modules` as it was
        try:
            backup.evict(accepted)
            importlib.invalidate_caches()
            retained = set(sys.modules)
            for item in stale:
                self._import(item, accepted)

            # the new graph is read once, before any module comes back
            new = self._edges.peek(backup.unloaded())
            units = partition(
                stale,
                [
                    item.modules | closure(item.roots(), new, retained.__contains__)
                    for item in stale
                ],
                accepted,
            )
            aborted = [u for u in units if any(i.error is not None for i in u.items)]
            skipped = [
                u
                for u in units
                if u not in aborted and any(i.skip is not None for i in u.items)
            ]
            committed = [u for u in units if u not in aborted and u not in skipped]
            kept = [*aborted, *skipped]

            self._adopt_unowned(units, committed, kept, accepted, old)
            restored = set().union(*(unit.modules for unit in kept))
            # a module that stage 1 loaded for the first time stays loaded, unless it
            # ran against the new version of a module that comes back
            for name in (name for item in stale for name in item.loaded):
                if closure([name], new, retained.__contains__) & restored:
                    backup.evict([name])
            backup.restore(restored)
        except BaseException:
            backup.rollback(present)
            raise

        for unit in aborted:
            failure = next(item.error for item in unit.items if item.error is not None)
            result.aborted.append(Abort(sorted(unit.modules), failure))
        for unit in skipped:
            reason = next(item.skip for item in unit.items if item.skip is not None)
            result.skipped.append(Skip(sorted(unit.modules), reason))

        applied = {item for unit in committed for item in unit.items}
        fibers = self._replace([item for item in stale if item in applied])
        self._edges.snapshot()
        return fibers

    def _classify(self, matched: set[str], result: ReloadResult) -> set[str]:
        """Return the changed modules that can reload, and record the skipped ones."""
        changed: set[str] = set()
        for name in sorted(matched):
            if name in self.host_modules:
                result.skipped.append(Skip([name], SkipReason.HOST_MODULE))
            elif _is_extension(name):
                result.skipped.append(Skip([name], SkipReason.EXTENSION_MODULE))
            else:
                changed.add(name)
        return changed

    def _stale_components(
        self, accepted: set[str], old: dict[str, tuple[str, ...]]
    ) -> list[_Stale]:
        """Describe the registered components that the accepted modules export."""
        loaded = [name for name in old if name in accepted]
        return [
            _stale(exporters, old, accepted)
            for exporters in find_components(loaded, self.ctx._registry).values()
        ]

    def _import(self, item: _Stale, accepted: set[str]) -> None:
        """Import the new modules of a component, and check its new value."""
        before = set(sys.modules)
        try:
            for name in item.entries[1:]:
                importlib.import_module(name)
            module = importlib.import_module(item.entry.module)
            value = item.entry.read(module)
            if self.ctx._registry.resolve(value) is None:  # type: ignore[arg-type]
                raise CordisError(
                    f"invalid component at {item.entry.module}, expect function or "
                    f'object with an "apply" method, received {type(value).__name__}'
                )
            item.replacement = value  # type: ignore[assignment]
            if _holds_other_value(item, value, self.ctx._registry):  # type: ignore[arg-type]
                item.skip = SkipReason.AMBIGUOUS_EXPORT
        except Exception as error:
            self._logger.warning(
                "failed to import %s", item.entry.module, exc_info=error
            )
            item.error = error
        item.loaded = [
            name
            for name in list(sys.modules)
            if name not in before and name not in accepted
        ]

    @staticmethod
    def _adopt_unowned(
        units: list[Unit[_Stale]],
        committed: list[Unit[_Stale]],
        kept: list[Unit[_Stale]],
        accepted: set[str],
        old: dict[str, tuple[str, ...]],
    ) -> None:
        """Add each evicted module that no unit owns to the kept units that it reaches.

        The module comes back with the kept units that it imports in the old graph, so
        that it links against the modules that they restore. When it also reaches a
        committed unit, neither version is consistent, and it stays evicted.
        """
        owned = set().union(*(unit.modules for unit in units))
        for name in sorted(accepted - owned):
            reached = closure([name], old, lambda other: other not in accepted)
            if any(not unit.modules.isdisjoint(reached) for unit in committed):
                continue
            for unit in kept:
                if not unit.modules.isdisjoint(reached):
                    unit.modules.add(name)

    def _replace(self, applied: list[_Stale]) -> list[Fiber]:
        """Replace each component with its new value, and return the moved fibers."""
        registry = self.ctx._registry
        fibers: list[Fiber] = []
        for item in applied:
            old, new = item.entry.value, item.replacement
            # a module that only imports its component by name runs the same function
            if new is None or registry.resolve(new) == registry.resolve(old):
                continue
            try:
                moved = registry.replace(old, new)
            except Exception as error:
                self._logger.warning(
                    "failed to reload component at %s",
                    item.entry.module,
                    exc_info=error,
                )
                continue
            if moved:
                self._logger.info("reload component at %s", item.entry.module)
                fibers.extend(moved)
        return fibers


def _release() -> None:
    """Give up the one active place of the process."""
    Hmr._active = False


def _is_extension(name: str) -> bool:
    """Tell whether the loaded module `name` is a C extension."""
    file = module_file(sys.modules[name])
    return file is not None and file.endswith(
        tuple(importlib.machinery.EXTENSION_SUFFIXES)
    )


def _holds_other_value(
    item: _Stale, value: Component, registry: RegistryService
) -> bool:
    """Tell whether an entry module holds a component other than `value`.

    A module that has an `apply` function stands for that function, so two places hold
    the same component when the registry resolves them to the same runner.
    """
    expected = registry.resolve(value)
    return any(
        exporter is not item.entry
        and exporter.module in item.entries
        and registry.resolve(exporter.read(sys.modules[exporter.module])) != expected  # type: ignore[arg-type]
        for exporter in item.exporters
    )


def _roots(paths: Iterable[Any]) -> set[str]:
    """Return the normalized absolute paths, or raise `TypeError` for another value."""
    roots: set[str] = set()
    for path in paths:
        text = os.fspath(path) if isinstance(path, os.PathLike) else path
        if not isinstance(text, str) or not os.path.isabs(text):
            shown = json.dumps(text, default=repr)
            raise TypeError(f"invalid root {shown}, expected an absolute path")
        roots.add(os.path.normpath(text))
    return roots


def _stale(
    exporters: list[Exporter],
    old: dict[str, tuple[str, ...]],
    accepted: set[str],
) -> _Stale:
    """Describe the component that `exporters` hold, with its old-graph closure."""
    names = list(dict.fromkeys(exporter.module for exporter in exporters))
    closures = {
        name: closure([name], old, lambda other: other not in accepted)
        for name in names
    }
    # the entries are the modules that no other module of the component imports
    entries = [
        name
        for name in names
        if not any(name in closures[other] for other in names if other != name)
    ] or names[:1]
    entry = next(exporter for exporter in exporters if exporter.module == entries[0])
    modules = set().union(*closures.values())
    return _Stale(entry, entries, exporters, modules)
