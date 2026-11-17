"""Entries: the nodes of a plugin tree.

An `Entry` holds the options of one node (a component name, a config, and per-entry
options) and the fiber that runs the component. `EntryOptions` is the data of a node,
and `EntryValidationError` reports options that are not valid.
"""

import random
from collections.abc import Coroutine, Mapping
from typing import TYPE_CHECKING, Any, TypedDict

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError, Issue, ValidationError
from pycordis.fiber import Fiber
from pycordis.scheduler import Scheduler, start

if TYPE_CHECKING:
    from pycordis.loader._group import EntryGroup
    from pycordis.loader._service import Loader
    from pycordis.loader._tree import EntryTree

ENTRY_SEP = ":"
"""The separator between the ids of the entries on the path to a nested entry."""


class EntryOptions(TypedDict, total=False):
    """The data of one entry.

    A field that is absent has no value: it is not the same as a field that is `None`.
    In an update, `None` deletes the field.

    Attributes:
        id: The id of the entry in its tree. It must not contain `":"`. The tree gives
            an entry without an `id` a random one.
        name: The component of the entry: `"package.module"`, or
            `"package.module:attribute"`. The import function of the tree resolves it.
        config: The config that the component receives. For a group, the list of the
            child entries.
        group: `True` for a structure: it is always enabled, its `config` is a list
            of child entries, and its `disabled` value applies to its children.
        disabled: `True` to keep the component unloaded.
        inject: The services that the entry needs before its component loads.
        intercept: The intercept configurations by service name.
        isolate: The realm of each service name, `True` or a label.
    """

    id: str
    name: str
    config: Any
    group: bool | None
    disabled: bool | None
    inject: list[str] | dict[str, Any] | None
    intercept: dict[str, Any] | None
    isolate: dict[str, bool | str] | None


class EntryValidationError(ValidationError):
    """Raised when entry options are not valid.

    The message is "invalid entry options" followed by one line for each issue.
    """

    def __init__(self, issues: "list[Issue]") -> None:
        super().__init__(issues)
        self.args = (
            "invalid entry options:" + str(self).removeprefix("invalid config:"),
        )


def validate_options(options: object, sep: str, deep: bool = False) -> list[Issue]:
    """Return the reasons that `options` is not a valid entry, or an empty list.

    Args:
        options: The value to check.
        sep: The separator that an `id` must not contain.
        deep: Also check each child entry of a group.
    """
    if not isinstance(options, Mapping):
        return [Issue("entry must be an object")]
    issues: list[Issue] = []

    def check(key: str, expected: str, valid: Any, required: bool = False) -> None:
        value = options.get(key)
        if value is None:
            if required:
                issues.append(Issue(f"{key} is required", (key,)))
        elif not valid(value):
            issues.append(Issue(f"{key} must be {expected}", (key,)))

    def is_string(value: object) -> bool:
        return isinstance(value, str)

    def is_bool(value: object) -> bool:
        return isinstance(value, bool)

    def is_object(value: object) -> bool:
        return isinstance(value, Mapping)

    def is_list(value: object) -> bool:
        return isinstance(value, list)

    check("id", "a string", is_string)
    identifier = options.get("id")
    if isinstance(identifier, str) and sep in identifier:
        issues.append(Issue(f'id must not contain "{sep}"', ("id",)))
    check("name", "a string", is_string, required=True)
    check("group", "a boolean", is_bool)
    check("disabled", "a boolean", is_bool)
    if options.get("group") is True:
        check("config", "an array", is_list, required=True)
    check("inject", "an array or an object", lambda v: is_list(v) or is_object(v))
    inject = options.get("inject")
    if isinstance(inject, list):
        issues.extend(
            Issue(f"inject.{index} must be a string", ("inject", index))
            for index, name in enumerate(inject)
            if not isinstance(name, str)
        )
    check("intercept", "an object", is_object)
    check("isolate", "an object", is_object)
    isolate = options.get("isolate")
    if isinstance(isolate, Mapping):
        issues.extend(
            Issue(f"isolate.{name} must be true or a string", ("isolate", name))
            for name, label in isolate.items()
            if label is not True and not isinstance(label, str)
        )
    config = options.get("config")
    if deep and options.get("group") is True and isinstance(config, list):
        for index, child in enumerate(config):
            issues.extend(
                Issue(issue.message, ("config", index, *issue.path))
                for issue in validate_options(child, sep, True)
            )
    return issues


def own_options(options: Mapping[str, Any], group: bool | None = None) -> EntryOptions:
    """Return a copy of `options` that the caller can keep.

    The copy is a new dict. For a group, `config` is a new list that holds a copy of
    each child entry. The `config` of another entry is not copied: the component owns
    it.

    Args:
        options: The options to copy.
        group: Whether the entry is a group. `None` reads it from `options`.
    """
    copy: dict[str, Any] = dict(options)
    if group is None:
        group = copy.get("group") is True
    config = copy.get("config")
    if group and isinstance(config, list):
        copy["config"] = [
            own_options(child) if isinstance(child, Mapping) else child
            for child in config
        ]
    return copy  # type: ignore[return-value]


def merge_options(target: EntryOptions, options: Mapping[str, Any]) -> EntryOptions:
    """Write `options` into `target`: a `None` value deletes the field."""
    for key, value in options.items():
        if value is None:
            target.pop(key, None)  # type: ignore[misc]
        else:
            target[key] = value  # type: ignore[literal-required]
    return target


def find_loader(ctx: Context) -> "Loader":
    """Return the loader service that `ctx` sees.

    Raises:
        CordisError: If no loader is provided.
    """
    loader = ctx.get("loader", strict=False)
    if loader is None:
        raise CordisError("the loader service is not provided")
    return loader


def new_id(taken: Mapping[str, object]) -> str:
    """Return a random eight-character hex id that is not a key of `taken`."""
    while True:
        identifier = f"{random.getrandbits(32):08x}"
        if identifier not in taken:
            return identifier


def run[T](scheduler: Scheduler, coroutine: Coroutine[Any, Any, T]) -> Deferred[T]:
    """Run the first step of `coroutine` now, and complete the rest on `scheduler`."""
    with scheduler.call():
        return start(scheduler, coroutine)


def _same_mapping(a: Mapping[str, object], b: Mapping[str, object]) -> bool:
    return a.keys() == b.keys() and all(a[key] is b[key] for key in a)


def _same_scope(a: Context, b: Context) -> bool:
    """Return whether components in `a` and in `b` see the same isolation and meta."""
    return a is b or (
        _same_mapping(a.isolates, b.isolates)
        and _same_mapping(a.intercepts, b.intercepts)
        and _same_mapping(a.meta, b.meta)
    )


class Entry:
    """One node of an `EntryTree`: its options and the fiber of its component.

    The tree creates and owns its entries. A host reads them through `EntryTree.resolve`
    and `EntryTree.entries`, and changes them with the tree methods `create`, `update`,
    and `remove`.

    The fiber of an entry is a child of the fiber of the tree that holds the entry, also
    when the entry is in a group. Its context has the isolation, intercepts, and
    `meta` of the context of its group. A change of `name`, or a move that changes the
    context of the entry or its tree, loads the component again in a new fiber. Any
    other change keeps the fiber.

    Attributes:
        loader: The loader that owns the tree.
        parent: The group that holds the entry.
        options: The options of the entry. The entry owns this dict.
        inner_fiber: The fiber of the component, or `None` before the first load. It
            can be disposed.
        subgroup: The group that the component of the entry holds, or `None`.
        subtree: The tree that the component of the entry holds, or `None`.
    """

    def __init__(self, loader: "Loader", parent: "EntryGroup") -> None:
        self.loader = loader
        self.parent = parent
        self.options: EntryOptions = {}
        self.inner_fiber: Fiber | None = None
        self.subgroup: EntryGroup | None = None
        self.subtree: EntryTree | None = None
        self._scheduler = loader.ctx.scheduler
        self._init_task: Deferred[None] | None = None
        self._disposing = False
        self._verdict: bool | None = None
        self._home: tuple[EntryTree, Context] | None = None

    @property
    def id(self) -> str:
        """The id of the entry, in a nested tree: the path of ids, joined by `:`."""
        identifier = self.options["id"]
        host = self.parent.tree.entry
        return identifier if host is None else f"{host.id}{ENTRY_SEP}{identifier}"

    @property
    def disabled(self) -> bool:
        """Whether the entry keeps its component unloaded.

        A group is never disabled: its `disabled` value applies to its children.
        """
        if self.options.get("group"):
            return False
        return self._verdict is not False or self._parent_disabled()

    @property
    def pending(self) -> Deferred[None] | None:
        """The task that loads the component again, or `None`."""
        return self._init_task

    def live_fiber(self) -> Fiber | None:
        """Return the fiber of the component while it is not disposed, else `None`."""
        fiber = self.inner_fiber
        return fiber if fiber is not None and fiber.uid is not None else None

    def _parent_disabled(self) -> bool:
        group = self.parent
        while group.entry is not None:
            if group.entry._verdict is not False:
                return True
            group = group.entry.parent
        return False

    def _blocked(self) -> bool:
        if self.options.get("group"):
            return False
        if self.options.get("disabled"):
            self._verdict = True
            return True
        return self._parent_disabled()

    def _resolve_disabled(self) -> None:
        self._verdict = bool(self.options.get("disabled"))

    def _registered(self) -> bool:
        return self.parent.tree.store.get(self.options["id"]) is self

    def _moved(self) -> bool:
        """Return whether the context of the live fiber differs from the group's."""
        assert self._home is not None
        tree, ctx = self._home
        return tree is not self.parent.tree or not _same_scope(ctx, self.parent.ctx)

    async def update(
        self, options: Mapping[str, Any], create: bool = False, force: bool = False
    ) -> None:
        """Apply `options` and make the fiber match the new state.

        Args:
            options: The new options. With `create`, they replace the options of the
                entry, and the entry owns the dict. Otherwise they are merged, and a
                `None` value deletes a field.
            create: Replace the options instead of merging them.
            force: Reconcile also when no option changed.
        """
        legacy = dict(self.options)
        if create:
            self.options = options  # type: ignore[assignment]
        else:
            merge_options(self.options, options)
        if self._blocked():
            self.dispose()
            return
        fiber = self.live_fiber()
        rebuild = self.options.get("name") != legacy.get("name") or (
            fiber is not None and self._moved()
        )
        if rebuild or fiber is None:
            # the disposal is part of the init task, so a concurrent update joins it
            await self.init(self.dispose() if fiber is not None else None)
            return
        keys = self.options.keys() | legacy.keys()
        diff = [key for key in keys if self.options.get(key) != legacy.get(key)]  # type: ignore[misc]
        if diff or force:
            await self._reconcile(fiber, diff)

    async def _reconcile(self, fiber: Fiber, diff: list[str]) -> None:
        self._resolve_disabled()
        if "config" in diff or self.options.get("group"):
            await fiber.update(self.options.get("config"), no_save=True)

    def dispose(self) -> Deferred[None] | None:
        """Dispose the fiber of the component. The loader does not report it."""
        return self._dispose(self.inner_fiber)

    def _dispose(self, fiber: Fiber | None) -> Deferred[None] | None:
        if fiber is None:
            return None
        self._disposing = True
        try:
            return fiber.dispose()
        finally:
            self._disposing = False

    async def init(self, dispose_task: Deferred[None] | None = None) -> None:
        """Load the component, after `dispose_task`, in a fiber of the tree.

        A call while another load runs waits for that load.
        """
        task = self._init_task
        if task is None:
            task = self._init_task = run(self._scheduler, self._init(dispose_task))
        try:
            await task
        finally:
            # `_init` releases the task before it creates the fiber, whose component
            # can start another
            if self._init_task is task:
                self._init_task = None

    async def _init(self, dispose_task: Deferred[None] | None) -> None:
        if dispose_task is not None:
            await dispose_task
        # a later update has already rebuilt the entry
        if self.live_fiber() is not None:
            return
        self._verdict = None
        name = self.options["name"]
        component = await self._import()
        # the name can change while the module is imported
        if name != self.options["name"]:
            return await self._init(None)
        if component is None or not self._registered() or self._blocked():
            return
        self._init_task = None
        self._start(component)

    async def _import(self) -> object | None:
        """Import the component of the entry. Log the failure and give `None`."""
        tree = self.parent.tree
        try:
            component = tree.import_function(self.options["name"])
            if isinstance(component, Deferred):
                component = await component
            if tree.ctx.registry.resolve(component) is None:  # type: ignore[arg-type]
                raise TypeError(
                    f"{self.options['name']!r} is not a component: expect a function, "
                    'a class, or an object with an "apply" method'
                )
        except Exception as error:
            self.parent.tree.ctx.fiber.logger.error(error)
            return None
        return component

    def _start(self, component: Any) -> None:
        # a group still starts: its verdict only reaches its children
        self._resolve_disabled()
        self.loader._show_log(self, "apply")
        tree = self.parent.tree
        group_ctx = self.parent.ctx
        ctx = group_ctx.bind_fiber(tree.ctx.fiber)
        # the fiber is linked before its component can run
        with self._scheduler.call():
            self.inner_fiber = ctx.use(component, self.options.get("config"))
            self.loader._link(self.inner_fiber, self)
        self._home = (tree, group_ctx)
