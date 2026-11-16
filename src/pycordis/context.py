"""Contexts.

A `Context` is the handle through which code registers effects. Derived contexts share
the fiber and the scheduler of their parent, and carry their own isolation, intercept,
and meta tables. Start with `Context(scheduler)` to create a root context.
"""

from collections import ChainMap
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Self

from pycordis.deferred import Deferred
from pycordis.errors import CallSite
from pycordis.events import EventName, EventOptions, EventsService, Listener
from pycordis.fiber import EffectForm, EffectHandle, Fiber
from pycordis.scheduler import Scheduler


class Context:
    """The place from which code registers effects.

    A root context owns the root fiber. `extend`, `isolate`, and `intercept` return new
    contexts that point to their parent: the parent does not change, and a table
    lookup reads the nearest context that has the key.

    Call context methods on the owner thread of the root context, which is the thread
    that created it.

    Examples:
        ```python
        >>> ctx = Context(ImmediateScheduler())
        >>> handle = ctx.effect(lambda: lambda: print("reverted"))
        >>> handle()
        reverted
        ```

    Attributes:
        scheduler: The scheduler that runs the work of the root context.
        root: The root context.
        fiber: The fiber that owns the effects that this context registers.
        events: The service that stores the listeners of the root context. Every
            derived context shares it.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self.root: Self = self
        self._isolates: ChainMap[str, object] = ChainMap()
        self._intercepts: ChainMap[str, object] = ChainMap()
        self._meta: ChainMap[str, object] = ChainMap()
        self.events = EventsService(scheduler)
        self.fiber = Fiber(self)

    def __repr__(self) -> str:
        return f"Context <{self.fiber.name}>"

    @property
    def isolates(self) -> Mapping[str, object]:
        """The isolation labels by service name, as a read-only mapping."""
        return MappingProxyType(self._isolates)

    @property
    def intercepts(self) -> Mapping[str, object]:
        """The intercept configurations by service name, as a read-only mapping."""
        return MappingProxyType(self._intercepts)

    @property
    def meta(self) -> Mapping[str, object]:
        """The values that `extend` added, as a read-only mapping."""
        return MappingProxyType(self._meta)

    def extend(self, **meta: object) -> Self:
        """Return a child context that adds `meta` to its `meta` mapping.

        A name in `meta` hides the same name in the parent's mapping.
        """
        return self._derive(
            self._isolates, self._intercepts, self._meta.new_child(meta)
        )

    def isolate(self, name: str, label: object | None = None) -> Self:
        """Return a child context whose service `name` is separate from the parent's.

        Contexts with the same label share the service. The default label is a new
        object, so the child context has a realm of its own.

        Args:
            name: The service name.
            label: The identity of the realm, or `None` for a new realm.
        """
        realm = object() if label is None else label
        return self._derive(
            self._isolates.new_child({name: realm}), self._intercepts, self._meta
        )

    def intercept(self, name: str, config: object) -> Self:
        """Return a child context that passes `config` to the service `name`."""
        return self._derive(
            self._isolates, self._intercepts.new_child({name: config}), self._meta
        )

    def effect(
        self, execute: Callable[[], EffectForm], label: str = "anonymous"
    ) -> EffectHandle:
        """Run `execute` and register its inverses on this context's fiber.

        The fiber reverts them when it unloads. See `Fiber.effect` for the forms that
        `execute` can return.

        Args:
            execute: The setup function.
            label: The name that `Fiber.get_effects` reports.

        Returns:
            The handle of the effect. Call it to revert the effect now.

        Raises:
            CordisError: If the fiber is disposed.
            TypeError: If `execute` returns an invalid effect.
        """
        with self.scheduler.call():
            return self.fiber.effect(execute, label, call_site=CallSite.capture(skip=1))

    def on(
        self,
        name: EventName,
        listener: Listener,
        options: EventOptions | bool = False,
    ) -> EffectHandle:
        """Register `listener` for the event `name` on this context's fiber.

        The fiber removes the listener when it unloads.

        Args:
            name: The event name.
            listener: The function to call with the arguments of the event.
            options: The `EventOptions`, or a `bool` for `prepend`.

        Returns:
            The handle of the effect. Call it to remove the listener.

        Raises:
            CordisError: If the fiber is disposed.

        Examples:
            ```python
            >>> remove = ctx.on("greet", lambda who: print("hello", who))
            >>> ctx.emit("greet", "world")
            hello world
            >>> remove()
            ```
        """
        return self.events.on(self, name, listener, options)

    def once(
        self,
        name: EventName,
        listener: Listener,
        options: EventOptions | bool = False,
    ) -> EffectHandle:
        """Register `listener` for the event `name`, and remove it after its first call.

        The arguments and the result are those of `on`.
        """
        return self.events.once(self, name, listener, options)

    def emit(self, name: EventName, *args: Any, this: object = None) -> None:
        """Call every listener of `name` with `args`, and ignore the results.

        Args:
            name: The event name.
            *args: The arguments for the listeners.
            this: An object with a `cordis_filter(ctx)` method that selects the
                listeners by the context that registered them.
        """
        self.events.emit(name, *args, this=this)

    def bail(self, name: EventName, *args: Any, this: object = None) -> Any:
        """Call the listeners in order, and return the first result that is not
        `None` or `False`.

        The arguments are those of `emit`.
        """
        return self.events.bail(name, *args, this=this)

    def serial(self, name: EventName, *args: Any, this: object = None) -> Deferred[Any]:
        """Call the listeners one after another, and wait for each result.

        The arguments are those of `emit`. The result is a `Deferred` of the first
        result that is not `None` or `False`.
        """
        return self.events.serial(name, *args, this=this)

    def parallel(
        self, name: EventName, *args: Any, this: object = None
    ) -> Deferred[None]:
        """Start every listener, and wait until all of them finish.

        The arguments are those of `emit`. The `Deferred` rejects with an
        `ExceptionGroup` when any listener fails.
        """
        return self.events.parallel(name, *args, this=this)

    def waterfall(self, name: EventName, *args: Any, this: object = None) -> Any:
        """Run the listeners as a chain around the final function in `args[-1]`.

        Each listener receives the other arguments and a `next` function that runs the
        rest of the chain. See `EventsService.waterfall`.
        """
        return self.events.waterfall(name, *args, this=this)

    def _derive(
        self,
        isolates: ChainMap[str, object],
        intercepts: ChainMap[str, object],
        meta: ChainMap[str, object],
    ) -> Self:
        child = object.__new__(type(self))
        child.scheduler = self.scheduler
        child.root = self.root
        child.fiber = self.fiber
        child.events = self.events
        child._isolates = isolates
        child._intercepts = intercepts
        child._meta = meta
        return child
