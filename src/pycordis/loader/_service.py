"""The loader service."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.events import EventOptions
from pycordis.fiber import Fiber, FiberState
from pycordis.loader._entry import Entry, is_outer, own_options, run
from pycordis.loader._tree import (
    EntryChange,
    EntryTree,
    ImportFunction,
    import_component,
)
from pycordis.service import Service, resolve_intercept
from pycordis.traceable import Tracker


@dataclass(frozen=True, slots=True)
class LoaderConfig:
    """The config of the `Loader`.

    Attributes:
        package: The package that an entry name starting with `.` is relative to.
        enable_logs: Log `apply`, `unload`, and `reload` of each entry that is not a
            group, to the `loader` logger.
    """

    package: str | None = None
    enable_logs: bool = False


class Loader(EntryTree, Service):
    """The service that keeps a tree of plugin entries and loads their components.

    A host whose plugin set comes from data gives the loader a list of entry
    options, and the loader makes the `use`, `dispose`, and `update` calls. Each entry
    is a component name, a config, and options (see `EntryOptions`). The loader owns
    the fibers that it creates. It copies the data that the host passes, so a later
    change to a host dict has no effect.

    The loader reports each change to `commit`, which does nothing by default. A host
    that saves its plugin set overrides `commit`. A component that disposes its own
    fiber disables its entry, and the loader reports that change. A removal by the
    loader, the unload of a group, and the disposal of a tree are not reported.

    A component that injects `loader` with `{"await": True}` (the `inject` option of
    an entry, or a layer of `Context.intercept`) uses the loader only while no entry
    loads, rebuilds, or has a fiber that loads or unloads (see `wait`).

    Call every method on the owner thread.

    Args:
        ctx: The context of the component.
        config: The `LoaderConfig`, or `None` for the default.
        import_function: Resolves an entry name to a component. The default imports
            `"package.module"` and `"package.module:attribute"` with `importlib`.

    Examples:
        ```python
        >>> fiber = ctx.use(Loader)
        >>> ctx.get("loader").read([{"id": "a", "name": "app.plugins:greet"}])
        ```

    Attributes:
        config: The `LoaderConfig`.
    """

    provide = "loader"

    def __init__(
        self,
        ctx: Context,
        config: LoaderConfig | None = None,
        *,
        import_function: ImportFunction | None = None,
    ) -> None:
        self.config = config or LoaderConfig()
        self._links: dict[Fiber, Entry] = {}
        EntryTree.__init__(
            self,
            ctx,
            import_function=import_function
            or partial(import_component, package=self.config.package),
        )
        Service.__init__(self, ctx)
        # A reader gets a view that shows the loader's own `ctx`: the loader acts for
        # its own context, whoever reads it. The view tells the check who reads.
        self.cordis_tracker = Tracker(no_shadow=True)  # type: ignore[assignment]
        self.enable_logs = self.config.enable_logs
        ctx.on("internal/status", self._on_status)
        ctx.on("internal/plugin", self._on_plugin)
        ctx.on("internal/commit", self._on_commit)
        ctx.on(
            "internal/update",
            self._on_update,
            EventOptions(prepend=True, is_global=True),
        )

    def _owner_loader(self) -> "Loader":
        return self

    def commit(self, change: EntryChange) -> None:
        """Do nothing: the root tree lives in memory. A host overrides it to save."""

    def cordis_check(self) -> bool:
        """Hold back a reader that sets `await` in its intercept configuration.

        A fiber whose `inject` (or intercept layer) for `loader` has a true `await`
        value uses the loader only while no entry loads, rebuilds, or has a fiber that
        loads or unloads. The loader checks the readers again when it becomes idle.
        """
        config = self.cordis_resolve_intercept()
        return not (config.get("await") and self.busy())

    def cordis_resolve_intercept(self) -> dict[str, Any]:  # type: ignore[override]
        """Return the intercept configuration for the reader of the loader."""
        caller = self.cordis_caller
        return resolve_intercept(self, self.ctx if caller is None else caller, None)

    def read(self, data: Sequence[Mapping[str, Any]]) -> Deferred[None]:
        """Replace the entries of the root group with `data`, and load them.

        The loader copies `data`. An entry that is not in `data` is removed, and the
        change is not reported to `commit`. An entry that is not valid is logged and
        skipped.

        Returns:
            A `Deferred` that completes when `wait` does.
        """
        config = [
            own_options(item) if isinstance(item, Mapping) else item for item in data
        ]
        return run(self.ctx.scheduler, self._read(config), self.ctx.fiber)  # type: ignore[arg-type]

    async def _read(self, config: list[Any]) -> None:
        await self.root.update(config)
        await self.wait()

    def busy(self) -> bool:
        """Return whether an entry loads, or has a fiber that loads or unloads.

        An entry that waits for a service is not busy.
        """
        return any(entry.busy() for entry in self.entries())

    def wait(self) -> Deferred[None]:
        """Wait until no entry loads its component or has a fiber that loads or unloads.

        An entry that waits for a service, through its `inject` option or the `inject`
        of its component, does not keep the wait open.

        Returns:
            A `Deferred` that completes when the whole tree is idle. A fiber that
            failed does not keep the wait open.
        """
        return run(self.ctx.scheduler, self._settled(), self.ctx.fiber)

    async def _settled(self) -> None:
        while True:
            tasks = [task for entry in self.entries() for task in entry.tasks()]
            if not tasks:
                return
            for task in tasks:
                try:
                    await task
                except Exception:
                    pass  # the fiber or the entry has logged its own failure

    def entry_of(self, fiber: Fiber) -> Entry | None:
        """Return the entry whose component runs in `fiber`, or `None`.

        Only the fiber of the component itself has an entry: a fiber that the
        component creates has none.
        """
        return self._links.get(fiber)

    def locate(self, fiber: Fiber | None = None) -> str | None:
        """Return the id of the entry that `fiber` runs under, or `None`.

        Args:
            fiber: The fiber to start from. The default is the fiber of the loader.
        """
        fiber = self.ctx.fiber if fiber is None else fiber
        while True:
            entry = self._links.get(fiber)
            if entry is not None:
                return entry.id
            parent = fiber.parent.fiber
            if parent is fiber:
                return None
            fiber = parent

    def _link(self, fiber: Fiber, entry: Entry) -> None:
        self._links[fiber] = entry

    def _show_log(self, entry: Entry, action: str) -> None:
        if entry.options.get("group") or not entry.parent.tree.enable_logs:
            return
        self.ctx.logger("loader").info("%s component %s", action, entry.options["name"])

    def _on_update(self, fiber: Fiber, config: object, next_: Any) -> Any:
        entry = self._links.get(fiber)
        if entry is not None:
            self._show_log(entry, "reload")
        return next_()

    def _on_status(self, fiber: Fiber, old_state: FiberState) -> None:
        """Forget the verdict of an entry when its outer fiber loses a service."""
        if fiber.state != FiberState.UNLOADING or not is_outer(fiber):
            return
        entry: Entry = fiber.config  # type: ignore[assignment]
        if entry.outer_fiber is fiber:
            entry._verdict = None

    def _on_commit(self, fiber: Fiber, config: object) -> None:
        entry = self._links.get(fiber)
        if entry is None:
            return
        legacy = own_options(entry.options)
        entry.options["config"] = config
        group = entry.parent
        group.tree.commit(
            EntryChange(
                entry.options["id"], group, None, own_options(entry.options), legacy
            )
        )

    def _on_plugin(self, fiber: Fiber) -> None:
        """Tell a disposal that the component asked for from one that the loader did."""
        # 1. the fiber is created
        if fiber.uid is not None:
            return
        entry = self._links.pop(fiber, None)
        # 2. the fiber is not the fiber of an entry's component. A fiber that the
        # component creates has no link.
        if entry is None:
            return
        group = entry.parent
        # 3. the tree of the entry is being disposed
        if group.tree.ctx.fiber.uid is None:
            return
        self._show_log(entry, "unload")
        # 4. the group of the entry, or the outer fiber of the entry, unloads and takes
        # the entry with it
        if FiberState.UNLOADING in (group.ctx.fiber.state, fiber.parent.fiber.state):
            return
        # 5. the loader removes the entry: it leaves the store before its disposal
        if group.tree.store.get(entry.options["id"]) is not entry:
            return
        # 6. the loader disposes the fiber: a config change, a disabled group, a rebuild
        if entry._disposing:
            return
        # 7. the component disposed its own fiber: LOAD-OPTIONS allows only this change
        legacy = own_options(entry.options)
        entry.options["disabled"] = True
        entry._verdict = True
        group.tree.commit(
            EntryChange(
                entry.options["id"], group, None, own_options(entry.options), legacy
            )
        )
        if entry.outer_fiber is not None:
            entry.dispose()
