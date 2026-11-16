"""Events.

Plugins and services talk to each other through named events. A listener belongs to
the fiber of the context that registers it, so the fiber removes the listener when it
unloads. Start with `Context.on`, `Context.emit`, and the other dispatch methods of
`Context`; `EventsService` is the object that stores the listeners.

An event name is any hashable object. Use a `str` for a public event. Use a unique
`object()` for a private event that no other code can name by accident.

The five dispatch modes differ in how they treat the listeners and their results:

- `emit` calls each listener and ignores the results.
- `parallel` starts all listeners and waits for all of them. It collects the errors.
- `serial` calls the listeners one after another, and stops at the first bailing result.
- `bail` is `serial` without waiting: it calls the listeners one after another and stops
  at the first bailing result.
- `waterfall` gives each listener a `next` function that runs the rest of the chain.

A result bails when it is neither `None` nor `False`.
"""

import inspect
import json
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import EffectHandle
from pycordis.scheduler import Scheduler, start

if TYPE_CHECKING:
    from pycordis.context import Context

type EventName = Hashable
"""The name of an event: a `str`, or any other hashable object for a private event."""

type DispatchMode = Literal["emit", "parallel", "serial", "bail", "waterfall"]
"""The way that a dispatch calls its listeners."""

type Listener = Callable[..., Any]
"""A function that handles an event. It receives the arguments of the dispatch."""

type InternalEventName = Literal[
    "internal/plugin",
    "internal/replace",
    "internal/status",
    "internal/service",
    "internal/update",
    "internal/commit",
    "internal/config",
    "internal/get",
    "internal/set",
    "internal/listener",
    "internal/dispatch",
]
"""The names of the events that the core services dispatch."""


def is_bailed(value: object) -> bool:
    """Return `True` when a listener result stops `serial` and `bail`.

    Every value except `None` and `False` bails, so `0` and an empty string bail.
    """
    return value is not None and value is not False


@dataclass(frozen=True, slots=True)
class EventOptions:
    """How a listener joins the list of listeners of an event.

    Attributes:
        prepend: Put the listener before the existing listeners, not after them.
        is_global: Call the listener also when the filter of a dispatch rejects the
            context that registered it.
    """

    prepend: bool = False
    is_global: bool = False


@dataclass(eq=False, slots=True)
class Hook:
    """One registered listener.

    Attributes:
        ctx: The context that registered the listener.
        callback: The listener function.
        prepend: Whether the listener was added before the others.
        is_global: Whether a dispatch filter skips the listener.
        removed: Whether the listener has been removed. A dispatch that is already
            running skips a removed listener.
    """

    ctx: "Context"
    callback: Listener
    prepend: bool = False
    is_global: bool = False
    removed: bool = False


def _describe(name: EventName) -> str:
    return json.dumps(name) if isinstance(name, str) else str(name)


async def _settled(value: object) -> object:
    """Wait for the result of a listener. Every result takes at least one step."""
    if inspect.iscoroutine(value) or isinstance(value, Deferred):
        return await value
    await Deferred.resolved(None)
    return value


class EventsService:
    """Stores the listeners of a root context and dispatches events to them.

    Use the methods of `Context` to register and dispatch. A dispatch gets a snapshot of
    the listeners when it starts: a listener that another listener adds during the
    dispatch waits for the next dispatch, and a listener that another listener removes
    during the dispatch does not run.

    Every dispatch method takes the event name and the arguments of the event. The
    keyword `this` names an object for filtering: when the object has a
    `cordis_filter(ctx)` method, a listener runs only if the method returns true for the
    context that registered it, or if the listener is global. The listeners never
    receive `this`.

    Attributes:
        scheduler: The scheduler that runs the asynchronous dispatches.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self._hooks: dict[EventName, list[Hook]] = {}

    def listener_counts(self) -> dict[EventName, int]:
        """Return the number of registered listeners of each event that has any."""
        return {name: len(hooks) for name, hooks in self._hooks.items()}

    def emit(self, name: EventName, *args: Any, this: object = None) -> None:
        """Call every listener of `name` with `args`, and ignore the results.

        An error that a listener raises stops the dispatch and goes to the caller.
        """
        for hook in self._resolve("emit", name, args, this):
            if not hook.removed:
                hook.callback(*args)

    def bail(self, name: EventName, *args: Any, this: object = None) -> Any:
        """Call the listeners of `name` in order, and return the first bailing result.

        Returns:
            The first result that is neither `None` nor `False`, or `None`.
        """
        for hook in self._resolve("bail", name, args, this):
            if hook.removed:
                continue
            result = hook.callback(*args)
            if is_bailed(result):
                return result
        return None

    def serial(self, name: EventName, *args: Any, this: object = None) -> Deferred[Any]:
        """Call the listeners of `name` one after another, and wait for each result.

        A listener can return a coroutine or a `Deferred`. The dispatch waits for it
        before it calls the next listener.

        Returns:
            A `Deferred` that resolves with the first bailing result, or `None`. It
            rejects with the first error of a listener.
        """
        hooks = self._resolve("serial", name, args, this)
        with self.scheduler.call():
            return start(self.scheduler, self._serial(hooks, args))

    def parallel(
        self, name: EventName, *args: Any, this: object = None
    ) -> Deferred[None]:
        """Start every listener of `name`, and wait until all of them finish.

        An error in one listener does not stop the others.

        Returns:
            A `Deferred` that resolves with `None`. It rejects with an
            `ExceptionGroup` of the listener errors when any listener failed.
        """
        hooks = self._resolve("parallel", name, args, this)
        with self.scheduler.call():
            runs = [start(self.scheduler, self._invoke(hook, args)) for hook in hooks]
            return start(self.scheduler, self._gather(runs, name))

    def waterfall(self, name: EventName, *args: Any, this: object = None) -> Any:
        """Run a chain of listeners that wrap a final function.

        The last of `args` is the final function. Each listener receives the other
        arguments followed by `next`. `next()` runs the rest of the chain, and the final
        function at its end, and gives the result. A listener that does not call `next`
        ends the chain.

        Returns:
            The result of the first listener.

        Raises:
            CordisError: If a listener calls its `next` more than once.
        """
        hooks = self._resolve("waterfall", name, args, this)
        if not hooks:
            return args[-1]()
        *params, inner = args
        index = 0

        def dispatch() -> Any:
            nonlocal index
            while index < len(hooks) and hooks[index].removed:
                index += 1
            if index == len(hooks):
                return inner()
            hook = hooks[index]
            index += 1
            called = False

            def next_() -> Any:
                nonlocal called
                if called:
                    raise CordisError("next() called multiple times")
                called = True
                return dispatch()

            return hook.callback(*params, next_)

        return dispatch()

    def on(
        self,
        ctx: "Context",
        name: EventName,
        listener: Listener,
        options: EventOptions | bool = False,
    ) -> EffectHandle:
        """Register `listener` for `name` as an effect of the fiber of `ctx`.

        Args:
            ctx: The context that registers the listener.
            name: The event name.
            listener: The function to call.
            options: The `EventOptions`, or a `bool` for `prepend`.

        Returns:
            The handle of the effect. Call it to remove the listener.

        Raises:
            CordisError: If the fiber of `ctx` is disposed.
        """
        opts = (
            options
            if isinstance(options, EventOptions)
            else EventOptions(prepend=options)
        )
        ctx.fiber.assert_active()
        result = self.bail("internal/listener", ctx, name, listener, opts)
        if result:
            return result
        return self._register(ctx, f"ctx.on({_describe(name)})", name, listener, opts)

    def once(
        self,
        ctx: "Context",
        name: EventName,
        listener: Listener,
        options: EventOptions | bool = False,
    ) -> EffectHandle:
        """Register `listener` for `name`, and remove it after its first call.

        The arguments and the result are those of `on`.
        """

        def call_once(*args: Any) -> Any:
            dispose()
            return listener(*args)

        dispose = self.on(ctx, name, call_once, options)
        return dispose

    def install(
        self,
        ctx: "Context",
        name: EventName,
        listener: Listener,
        prepend: bool = False,
    ) -> None:
        """Register `listener` for `name` without a fiber, so that nothing removes it.

        A root context uses it for the listeners that its own services need. The
        listener is not reported to `internal/listener` listeners. It is global: a
        dispatch filter does not skip it.
        """
        hooks = self._hooks.setdefault(name, [])
        hook = Hook(ctx, listener, prepend, True)
        if prepend:
            hooks.insert(0, hook)
        else:
            hooks.append(hook)

    def _resolve(
        self, mode: DispatchMode, name: EventName, args: tuple[Any, ...], this: object
    ) -> list[Hook] | tuple[()]:
        """Return the listeners that a dispatch calls, and report the dispatch."""
        internal = isinstance(name, str) and name.startswith("internal/")
        if not internal and self._hooks.get("internal/dispatch"):
            self.emit("internal/dispatch", mode, name, args, this)
        hooks = self._hooks.get(name)
        if not hooks:
            return ()
        accept = getattr(this, "cordis_filter", None)
        if accept is None:
            return list(hooks)
        return [hook for hook in hooks if hook.is_global or accept(hook.ctx)]

    async def _serial(
        self, hooks: list[Hook] | tuple[()], args: tuple[Any, ...]
    ) -> Any:
        for hook in hooks:
            if hook.removed:
                continue
            result = await _settled(hook.callback(*args))
            if is_bailed(result):
                return result
        return None

    async def _invoke(self, hook: Hook, args: tuple[Any, ...]) -> None:
        if not hook.removed:
            await _settled(hook.callback(*args))

    async def _gather(self, runs: list[Deferred[None]], name: EventName) -> None:
        errors: list[Exception] = []
        for run in runs:
            try:
                await run
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup(
                f"one or more listeners of event {_describe(name)} failed", errors
            )

    def _register(
        self,
        ctx: "Context",
        label: str,
        name: EventName,
        callback: Listener,
        options: EventOptions,
    ) -> EffectHandle:
        def setup() -> Callable[[], None]:
            hooks = self._hooks.setdefault(name, [])
            hook = Hook(ctx, callback, options.prepend, options.is_global)
            if options.prepend:
                hooks.insert(0, hook)
            else:
                hooks.append(hook)
            return lambda: self._unregister(name, hook)

        return ctx.fiber.effect(setup, label)

    def _unregister(self, name: EventName, hook: Hook) -> None:
        hook.removed = True
        hooks = self._hooks.get(name)
        if hooks is None or hook not in hooks:
            return
        hooks.remove(hook)
        if not hooks:
            del self._hooks[name]
