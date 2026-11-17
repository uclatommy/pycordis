"""The `Hmr` service: a transaction that reloads modules and moves component fibers."""

import contextlib
import importlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import Fiber
from pycordis.hmr._graph import (
    EdgeCache,
    ModuleGraph,
    accepted_modules,
    closure,
    source_graph,
)
from pycordis.hmr._modules import Backup, Exporter, find_components, match_roots
from pycordis.logger import Logger
from pycordis.registry import Component
from pycordis.scheduler import owner_thread, start
from pycordis.service import Service

type Changes = Callable[
    [], Iterable[str | os.PathLike[str]] | Deferred[Iterable[str | os.PathLike[str]]]
]


class SkipReason(StrEnum):
    """Why a change cannot apply without a restart.

    Attributes:
        HOST_MODULE: The module belongs to the host, which runs once.
        EXTENSION_MODULE: The module is a C extension, which a process cannot load
            again.
        AMBIGUOUS_EXPORT: The places that held one component now hold different
            values.
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

    Attributes:
        modules: The names of the changed module, or of the modules of the unit.
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
    """

    scope: tuple[str, ...] = ()


@dataclass(slots=True)
class _Stale:
    """A registered component that a changed module exports."""

    entry: Exporter
    """The place that the replacement comes from."""
    entries: list[str]
    """The modules that no other module of the component imports, `entry` first."""
    modules: set[str]
    """The closure of the component in the old module graph."""
    replacement: Component | None = None
    error: BaseException | None = None


class Hmr(Service):
    """The service that reloads changed modules and moves component fibers.

    A development tool or an interactive host that knows which files changed calls
    `reconcile`. The service finds the loaded modules that the change affects, imports
    them again, and moves the live fibers of the components that they export to the new
    code with `RegistryService.replace`. A fiber keeps its identity, its config, and its
    place in the fiber tree.

    A transaction runs in three stages. It imports the new modules and checks each new
    component. If an import fails, or a new value is not a component, it restores the
    old modules and applies nothing. Otherwise it replaces the components, and then
    waits for the moved fibers. A new component whose `apply` raises leaves its fiber
    `FAILED`, and the next change of its file retries it.

    The service sees the modules that its graph function covers (by default the
    modules below `HmrConfig.scope`, outside the Python installation). A module outside
    the graph never reloads.

    Call every method on the owner thread.

    Args:
        ctx: The context of the component.
        config: The `HmrConfig`, or `None` for the default.
        graph: The `ModuleGraph` function that gives the imports of a module. The
            default is `source_graph` over `HmrConfig.scope`.

    Examples:
        ```python
        >>> fiber = ctx.use(Hmr)
        >>> result = ctx.get("hmr").reconcile(lambda: ["/app/plugins/greet.py"])
        >>> result.done()
        True
        ```

    Attributes:
        config: The `HmrConfig`.
    """

    provide = "hmr"

    def __init__(
        self,
        ctx: Context,
        config: HmrConfig | None = None,
        *,
        graph: ModuleGraph | None = None,
    ) -> None:
        super().__init__(ctx)
        self.config = config or HmrConfig()
        scope = self.config.scope or (os.getcwd(),)
        self._edges = EdgeCache(graph or source_graph(scope))
        self._tail: Deferred[None] | None = None

    @property
    def _logger(self) -> Logger:
        return self.ctx.logger("hmr")

    def cordis_init(self) -> None:
        """Record the imports of the loaded modules."""
        self._edges.snapshot()

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

        Transactions run one at a time: a call waits for the running transaction.
        If `changes` raises, or returns a `Deferred` that rejects, the transaction
        rejects with that error and `sys.modules` is as it was.

        The import of a module runs on the owner thread and blocks it until the import
        ends.

        Args:
            changes: A function that returns the changed paths, or a `Deferred` of them.

        Returns:
            A `Deferred` that resolves with the `ReloadResult` after the moved fibers
            settle. An error from an import, from `RegistryService.replace`, or from a
            moved fiber goes to the log and does not reject it.

        Raises:
            TypeError: In the returned `Deferred`, for a path that is not absolute.
        """
        previous = self._tail
        done = Deferred[None]()
        self._tail = done
        with self.ctx.scheduler.call():
            return start(self.ctx.scheduler, self._transaction(previous, done, changes))

    async def _transaction(
        self, previous: Deferred[None] | None, done: Deferred[None], changes: Changes
    ) -> ReloadResult:
        try:
            if previous is not None:
                await previous
            produced = changes()
            if isinstance(produced, Deferred):
                produced = await produced
            return await self._reconcile(_roots(produced))
        finally:
            done.resolve(None)

    async def _reconcile(self, roots: set[str]) -> ReloadResult:
        result = ReloadResult()
        old = self._edges.snapshot()
        changed = match_roots(roots, old)
        if not changed:
            return result
        accepted = accepted_modules(changed, old)
        registry = self.ctx.registry

        stale: list[_Stale] = []
        owned: set[str] = set()
        loaded = [name for name in old if name in accepted]
        for exporters in find_components(loaded, registry).values():
            item = _stale(exporters, old, accepted)
            owned |= item.modules
            stale.append(item)
        for name in sorted(changed - owned):
            self._logger.debug("evict %s (no dependent component)", name)

        backup = Backup()
        backup.evict(accepted)
        importlib.invalidate_caches()
        for item in stale:
            self._import(item)

        failed = [item for item in stale if item.error is not None]
        if failed:
            result.aborted.append(self._abort(failed[0], backup, owned, accepted, old))
            self._edges.snapshot()
            return result

        fibers = self._replace(stale)
        self._edges.snapshot()
        for fiber in fibers:
            # the fiber has logged its own failure
            with contextlib.suppress(Exception):
                await fiber.wait()
        result.settled.extend(fiber for fiber in fibers if fiber.uid is not None)
        return result

    def _import(self, item: _Stale) -> None:
        """Import the new modules of a component, and check its new value."""
        try:
            for name in item.entries[1:]:
                importlib.import_module(name)
            module = importlib.import_module(item.entry.module)
            value = item.entry.read(module)
            if self.ctx.registry.resolve(value) is None:  # type: ignore[arg-type]
                raise CordisError(
                    f"invalid component at {item.entry.module}, expect function or "
                    f'object with an "apply" method, received {type(value).__name__}'
                )
            item.replacement = value  # type: ignore[assignment]
        except Exception as error:
            self._logger.warning(
                "failed to import %s", item.entry.module, exc_info=error
            )
            item.error = error

    def _abort(
        self,
        failure: _Stale,
        backup: Backup,
        owned: set[str],
        accepted: set[str],
        old: dict[str, tuple[str, ...]],
    ) -> Abort:
        """Restore the old modules of the unit, and describe the abort.

        An evicted module that no component owns is restored when it reaches a module
        of the unit in the old graph, so that it links against the modules that come
        back.
        """
        restored = set(owned)
        for name in accepted - owned:
            if closure([name], old, lambda other: other not in accepted) & owned:
                restored.add(name)
        backup.restore(restored)
        assert failure.error is not None
        return Abort(sorted(restored), failure.error)

    def _replace(self, stale: list[_Stale]) -> list[Fiber]:
        """Replace each component with its new value, and return the moved fibers."""
        registry = self.ctx.registry
        fibers: list[Fiber] = []
        for item in stale:
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
    return _Stale(entry, entries, modules)
