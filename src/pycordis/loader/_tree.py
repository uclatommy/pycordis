"""Entry trees: the entries of a loader, and the reports of their changes.

An `EntryTree` holds a tree of plugin entries and reconciles the fibers with it. The
`Loader` is the root tree. A component can subclass `EntryTree` to hold a nested tree.
"""

import importlib
import importlib.util
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.loader._entry import (
    ENTRY_SEP,
    Entry,
    EntryOptions,
    EntryValidationError,
    find_loader,
    merge_options,
    new_id,
    own_options,
    run,
    validate_options,
)
from pycordis.loader._group import EntryGroup
from pycordis.traceable import Exposure

if TYPE_CHECKING:
    from pycordis.loader._service import Loader

type ImportFunction = Callable[[str], object | Deferred[object]]
"""Resolves the `name` of an entry to a component.

The function returns the component, or a `Deferred` of it. It raises an error, or the
`Deferred` rejects, when the name has no component.
"""

_UNSET: Any = object()


def import_component(name: str, package: str | None = None) -> object:
    """Import the component that `name` names.

    `name` is `"package.module"` or `"package.module:attribute"`. A name that starts
    with `.` is relative to `package`. The part after `:` is a path of attributes. A
    module without `:` is the component itself, when it has an `apply` function.

    Args:
        name: The name of the component.
        package: The package that a relative name starts from.

    Raises:
        ModuleNotFoundError: If the module of `name` does not exist. A module that the
            component imports and that does not exist raises its own error.
    """
    module_name, _, path = name.partition(":")
    try:
        component: object = importlib.import_module(module_name, package)
    except ModuleNotFoundError as error:
        resolved = importlib.util.resolve_name(module_name, package)
        if error.name is not None and (
            resolved == error.name or resolved.startswith(f"{error.name}.")
        ):
            raise ModuleNotFoundError(
                f"cannot find module {resolved!r}", name=error.name
            ) from None
        raise
    for attribute in filter(None, path.split(".")):
        component = getattr(component, attribute)
    return component


@dataclass(frozen=True, slots=True)
class EntryChange:
    """One change of a tree, reported to `EntryTree.commit` after it is applied.

    `options` and `legacy` are copies, so the host can keep them.

    - `options` is `None`: the entry was removed from `group`.
    - `legacy` is `None`: the entry was created in `group`.
    - Both are set: the entry was updated and is now in `group`. `moved_from` is the
      group that it left, when the update moved it inside the same tree.

    A move into another tree is a removal in the source tree and a creation in the
    target tree.

    Attributes:
        id: The id of the entry in its tree.
        group: The group that holds the entry after the change, or held it before a
            removal.
        moved_from: The group that the entry left, or `None`.
        options: The options after the change, or `None` for a removal.
        legacy: The options before the change, or `None` for a creation.
    """

    id: str
    group: EntryGroup
    moved_from: EntryGroup | None = None
    options: EntryOptions | None = None
    legacy: EntryOptions | None = None


def _members(entry: Entry) -> list[Entry]:
    """Return `entry` and the entries of its group that its tree holds, deeply."""
    tree = entry.parent.tree
    result = [entry]
    group = entry.subgroup
    if group is None or group.tree is not tree:
        return result
    for options in group.data:
        member = tree.store.get(options["id"])
        if member is not None and member.parent is group:
            result.extend(_members(member))
    return result


class EntryTree(ABC):
    """A tree of plugin entries that reconciles the fiber tree with its data.

    The root group holds a list of entry options. An entry whose component is a
    `Group` (or another `EntryTree`) holds more entries. The tree creates a fiber for
    each enabled entry, and keeps the fibers when the data changes in a way that does
    not need a new fiber. The host changes the data with `create`, `update`, and
    `remove`, and saves each change in `commit`.

    A component can subclass `EntryTree` to hold a nested tree. The component class
    calls `EntryTree.__init__` with its context, and the entries of the tree run in
    fibers of that component. A subclass implements `commit`, and can set
    `import_function`.

    Attributes:
        ctx: The context of the tree. Its fiber owns the fibers of the entries.
        loader: The loader that owns the tree.
        entry: The entry whose component is this tree, or `None` for the loader.
        root: The group at the top of the tree.
        store: The entries of this tree by id, without the entries of nested trees.
        import_function: The function that resolves an entry name to a component.
        enable_logs: Log `apply`, `unload`, and `reload` of each entry that is not a
            group.
    """

    sep = ENTRY_SEP
    cordis_expose = Exposure()

    def __init__(
        self, ctx: Context, *, import_function: ImportFunction | None = None
    ) -> None:
        self.ctx = ctx
        self.store: dict[str, Entry] = {}
        self.import_function: ImportFunction = import_function or import_component
        self.enable_logs = False
        self.loader: Loader = self._owner_loader()
        self.entry = self.loader.entry_of(ctx._fiber)
        self.root = EntryGroup(ctx, self)
        if self.entry is not None:
            self.entry.subtree = self

    def _owner_loader(self) -> "Loader":
        return find_loader(self.ctx)

    def entries(self) -> Iterator[Entry]:
        """Yield every entry of the tree, and of the nested trees, in order."""
        for entry in list(self.store.values()):
            yield entry
            if entry.subtree is not None:
                yield from entry.subtree.entries()

    def ensure_id(self, options: EntryOptions) -> str:
        """Return the `id` of `options`. An entry without an `id` gets a random one."""
        if not options.get("id"):
            options["id"] = new_id(self.store)
        return options["id"]

    def resolve(self, identifier: str) -> Entry:
        """Return the entry with the id `identifier`.

        The id of an entry in a nested tree has the ids on its path, joined by `:`.

        Raises:
            ValueError: If no entry has the id.
        """
        *parts, final = identifier.split(ENTRY_SEP)
        tree: EntryTree = self
        for part in parts:
            entry = tree.store.get(part)
            if entry is None or entry.subtree is None:
                raise ValueError(f"cannot resolve entry {identifier}")
            tree = entry.subtree
        entry = tree.store.get(final)
        if entry is None:
            raise ValueError(f"cannot resolve entry {identifier}")
        return entry

    def resolve_group(self, identifier: str | None) -> EntryGroup:
        """Return the group of the entry `identifier`, or the root group for `None`.

        Raises:
            ValueError: If no entry has the id, or the entry is not a group.
        """
        if not identifier:
            return self.root
        entry = self.resolve(identifier)
        if entry.subgroup is None:
            raise ValueError(f"entry {identifier} is not a group")
        return entry.subgroup

    def create(
        self,
        options: Mapping[str, Any],
        parent: str | None = None,
        position: int | None = None,
    ) -> Deferred[str]:
        """Add an entry to a group, report it, and load its component.

        The tree copies `options`, so a later change to the dict of the caller has no
        effect.

        Args:
            options: The options of the entry. Without an `id`, the tree gives one.
            parent: The id of the group, or `None` for the root group.
            position: The index in the group, or `None` for the end.

        Returns:
            A `Deferred` of the id of the entry. It completes after the component has
            been started, and it rejects with the error that stops the load.

        Raises:
            EntryValidationError: If `options` is not valid. Nothing changes.
            ValueError: If `parent` names no group.
        """
        group = self.resolve_group(parent)
        issues = validate_options(options, ENTRY_SEP, True)
        if issues:
            raise EntryValidationError(issues)
        owned = own_options(options)
        identifier = group.tree.ensure_id(owned)
        group.data.insert(len(group.data) if position is None else position, owned)
        group.tree.commit(EntryChange(identifier, group, None, own_options(owned)))
        return run(self.ctx.scheduler, group.create(owned), self.ctx._fiber)

    def remove(self, identifier: str) -> None:
        """Remove an entry: dispose its fiber, and report the removal.

        Raises:
            ValueError: If no entry has the id.
        """
        entry = self.resolve(identifier)
        group = entry.parent
        legacy = own_options(entry.options)
        group.remove(legacy["id"])
        group.tree.commit(EntryChange(legacy["id"], group, None, None, legacy))

    def update(
        self,
        identifier: str,
        options: Mapping[str, Any],
        parent: str | None = _UNSET,
        position: int | None = None,
    ) -> Deferred[None]:
        """Change the options of an entry, move it, or both, and report the change.

        A `None` value in `options` deletes the field. The entry keeps its fiber when
        only `config`, `disabled`, or `group` change, and when it moves to a group
        that has the same isolation, intercepts, and `meta`. A new `name`, a move that
        changes them, and a move to another tree load the component again in a new
        fiber. A move to another tree also moves the entries of a moved group.

        Args:
            identifier: The id of the entry.
            options: The changes.
            parent: The id of the target group. Without it the entry stays in its
                group. `None` is the root group.
            position: The index in the target group, or `None` for the end.

        Returns:
            A `Deferred` that completes when the fiber has settled.

        Raises:
            EntryValidationError: If the merged options are not valid. Nothing
                changes.
            ValueError: If an id is unknown, the target is not a group, or the target
                tree has an entry with the id of a moved entry.
        """
        entry = self.resolve(identifier)
        source = entry.parent
        legacy = own_options(entry.options)
        merged = merge_options(dict(legacy), options)  # type: ignore[arg-type]
        issues = validate_options(merged, ENTRY_SEP, True)
        if issues:
            raise EntryValidationError(issues)
        if parent is not _UNSET:
            self._move(entry, self.resolve_group(parent), position)
        owned = own_options(options, merged.get("group") is True)
        # `Entry.update` assigns the options before its first wait, so the change is
        # visible to `commit` when the call returns
        task = run(
            self.ctx.scheduler, entry.update(owned, False, True), self.ctx._fiber
        )
        group = entry.parent
        if group.tree is not source.tree:
            source.tree.commit(EntryChange(legacy["id"], source, None, None, legacy))
            group.tree.commit(
                EntryChange(legacy["id"], group, None, own_options(entry.options))
            )
        else:
            moved_from = None if group is source else source
            group.tree.commit(
                EntryChange(
                    legacy["id"], group, moved_from, own_options(entry.options), legacy
                )
            )
        return task

    @staticmethod
    def _move(entry: Entry, target: EntryGroup, position: int | None) -> None:
        source = entry.parent
        moved = [] if target.tree is source.tree else _members(entry)
        for member in moved:
            if member.options["id"] in target.tree.store:
                raise ValueError(
                    f"entry {member.options['id']} already exists in the target tree"
                )
        source.unlink(entry.options)
        target.data.insert(
            len(target.data) if position is None else position, entry.options
        )
        entry.parent = target
        # the target tree owns the moved entries from now on, so that it adopts them
        # when it reconciles its source
        for member in moved:
            del source.tree.store[member.options["id"]]
            target.tree.store[member.options["id"]] = member
            group = member.subgroup
            if group is not None and group.tree is source.tree:
                group.tree = target.tree

    @abstractmethod
    def commit(self, change: EntryChange) -> None:
        """Save one change of this tree.

        The tree calls it after the change is applied, on the owner thread. A host that
        stores its entries overrides it. The call must not raise.

        If it raises for a change that a fiber makes itself (`fiber.update`, or a
        component that disposes its own fiber), the loader logs the `Exception` with the
        logger of its own fiber. The change stays applied in memory but is not saved. If
        it raises for a call of `create`, `update`, or `remove`, the error goes to the
        caller of that method.
        """
