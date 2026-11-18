"""Groups: the lists of entries that a tree holds.

An `EntryGroup` reconciles a list of entry options with the entries of its tree. The
`Group` component holds a list of child entries inside an entry.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from pycordis.context import Context
from pycordis.errors import CordisError
from pycordis.loader._entry import (
    ENTRY_SEP,
    Entry,
    EntryOptions,
    EntryValidationError,
    find_loader,
    run,
    validate_options,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pycordis.loader._tree import EntryTree


class EntryGroup:
    """A list of entry options, and the entries that run them.

    The root group of a tree and the `Group` component are groups. A group keeps the
    list of options in `data`, in the order of the host. Its entries run in the fibers
    of its tree, not in a fiber of the group.

    Attributes:
        ctx: The context of the group. The entries of the group get the isolation,
            intercepts, and `meta` of this context.
        tree: The tree that holds the group.
        entry: The entry whose component holds the group, or `None` for the group of
            the root tree.
        data: The options of the entries, in order.
    """

    def __init__(self, ctx: Context, tree: "EntryTree") -> None:
        self.ctx = ctx
        self.tree = tree
        self.entry: Entry | None = tree.loader.entry_of(ctx.fiber)
        self.data: list[EntryOptions] = []
        if self.entry is not None:
            self.entry.subgroup = self

    async def create(self, options: EntryOptions) -> str:
        """Create or update the entry for `options`, and load its component.

        The group keeps `options` as the options of the entry. An entry that exists
        under the same id (for example one that moved here) is reused.

        Returns:
            The id of the entry.

        Raises:
            EntryValidationError: If `options` is not valid. An entry that has the
                same id is removed.
        """
        issues = validate_options(options, ENTRY_SEP)
        if issues:
            raise self._reject(options, issues)
        identifier = self.tree.ensure_id(options)
        entry = self.tree.store.get(identifier)
        if entry is None:
            entry = self.tree.store[identifier] = Entry(self.tree.loader, self)
        # the entry can come from another group
        entry.parent = self
        await entry.update(options, create=True, force=True)
        return entry.id

    def _reject(self, options: object, issues: list[Any]) -> EntryValidationError:
        error = EntryValidationError(issues)
        identifier = options.get("id") if isinstance(options, Mapping) else None
        if identifier is None:
            return error
        # unregistered first, so the disposal is not taken for a self-dispose
        entry = self.tree.store.pop(identifier, None)
        if entry is not None:
            entry.dispose()
        return error

    def unlink(self, options: EntryOptions) -> None:
        """Remove `options` from `data`. It is found by identity, not by value."""
        for index, item in enumerate(self.data):
            if item is options:
                del self.data[index]
                return

    def remove(self, identifier: str, is_dispose: bool = False) -> None:
        """Dispose the entry `identifier` of this group, and forget it.

        The entry leaves the store of the tree before its fiber is disposed: the loader
        does not take the disposal for a self-dispose.

        Args:
            identifier: The id of the entry in the tree.
            is_dispose: Keep the options in `data`.
        """
        entry = self.tree.store.get(identifier)
        # an entry that another group adopted belongs to that group now
        if entry is None or entry.parent is not self:
            return
        del self.tree.store[identifier]
        entry.dispose()
        if not is_dispose:
            self.unlink(entry.options)

    async def update(self, config: list[EntryOptions]) -> None:
        """Make the entries match `config`, and wait until each one has loaded.

        The group keeps `config` as its `data`. An entry that is not in `config` is
        removed. A child that fails is logged and does not stop the others.
        """
        old = self.data
        self.data = config
        old_map = {
            item["id"]: item
            for item in old
            if isinstance(item, Mapping) and "id" in item
        }
        # an entry without an id has no key to match, so each one is a new entry
        new_items: dict[object, EntryOptions] = {
            item.get("id", object()) if isinstance(item, Mapping) else object(): item
            for item in config
        }
        runs = []
        for key in dict.fromkeys([*old_map, *new_items]):
            if key in new_items:
                runs.append(
                    run(
                        self.ctx.scheduler,
                        self._create_logged(new_items[key]),
                        self.ctx.fiber,
                    )
                )
            else:
                self.remove(key)  # type: ignore[arg-type]
        for task in runs:
            await task

    async def _create_logged(self, options: EntryOptions) -> None:
        try:
            await self.create(options)
        except Exception as error:
            self.tree.ctx.fiber.logger.error(error)

    def stop(self) -> None:
        """Dispose every entry of the group, and keep the options in `data`."""
        for options in list(self.data):
            self.remove(options["id"], True)


class Group(EntryGroup):
    """The component that holds a list of child entries.

    Name an entry `"pycordis.loader:Group"` and give it a list of child entries as its
    `config`. The children run in the fibers of the tree of the entry, with the
    isolation, intercepts, and `meta` of the group. Their `disabled` option follows the
    group: an entry whose group is disabled does not load. A new config for the group
    reconciles the children, and the update ends when they have loaded. The group
    removes its children when it unloads.

    Examples:
        ```python
        >>> loader.read([
        ...     {"id": "box", "name": "pycordis.loader:Group", "group": True,
        ...      "config": [{"id": "a", "name": "app:plugin"}]},
        ... ])
        ```
    """

    def __init__(self, ctx: Context, config: Sequence[EntryOptions]) -> None:
        entry = find_loader(ctx).entry_of(ctx.fiber)
        if entry is None:
            raise CordisError("Group must run as the component of an entry")
        super().__init__(ctx, entry.parent.tree)
        self.config = config
        ctx.on("internal/update", lambda fiber, config, next_: self._take(config))

    def _take(self, config: list[EntryOptions]) -> Any:
        # the group takes the update itself: it reconciles the children in place
        return run(self.ctx.scheduler, self.update(config), self.ctx.fiber)

    async def cordis_init(self) -> "AsyncIterator[Any]":
        """Remove the children when the group unloads, and load the children."""
        yield self.stop
        await self.update(self.config)  # type: ignore[arg-type]
