"""Contexts.

A `Context` is the handle through which code registers effects. Derived contexts share
the fiber and the scheduler of their parent, and carry their own isolation, intercept,
and meta tables. Start with `Context(scheduler)` to create a root context.
"""

from collections import ChainMap
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType
from typing import Any, Self

from pycordis.deferred import Deferred
from pycordis.errors import CallSite
from pycordis.events import EventName, EventOptions, EventsService, Listener
from pycordis.fiber import EffectForm, EffectHandle, Fiber, resolve_config
from pycordis.keys import Keyable
from pycordis.reflect import Accessor, ReflectService
from pycordis.registry import Component, RegistryService
from pycordis.scheduler import Scheduler


def _run_update_hooks(fiber: Fiber, config: object, next_: Callable[[], Any]) -> Any:
    """Run the `internal/update` listeners of `fiber`, and then the rest of the chain.

    A listener that a fiber registered belongs to that fiber, so the chain of an update
    contains only the listeners of the fiber that updates. A listener that another
    listener removes during the chain is skipped.
    """
    hooks = list(fiber.update_hooks)

    def advance() -> Any:
        if not hooks:
            return next_()
        return hooks.pop(0)(fiber, config, advance)

    return advance()


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
        reflect: The service that stores the providers and accessors of the root
            context. Every derived context shares it.
        registry: The service that records the loaded components of the root context.
            Every derived context shares it.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self.root: Self = self
        self._isolates: ChainMap[str, object] = ChainMap()
        self._intercepts: ChainMap[str, object] = ChainMap()
        self._meta: ChainMap[str, object] = ChainMap()
        self.events = EventsService(scheduler)
        self.reflect = ReflectService(scheduler)
        self.registry = RegistryService(scheduler)
        self.fiber = Fiber(self)
        self.events.install(
            self,
            "internal/config",
            lambda fiber, next_: resolve_config(fiber.runtime.config, next_()),
        )
        self.events.install(self, "internal/listener", self._route_update_listener)
        self.events.install(self, "internal/update", _run_update_hooks, prepend=True)

    def __repr__(self) -> str:
        return f"Context <{self.fiber.name}>"

    @staticmethod
    def _route_update_listener(
        ctx: "Context", name: EventName, listener: Listener, options: EventOptions
    ) -> EffectHandle | None:
        """Keep an `internal/update` listener on the fiber that registers it."""
        if name != "internal/update" or options.is_global:
            return None

        def setup() -> Callable[[], None]:
            removed = False

            def guarded(fiber: Fiber, config: object, next_: Callable[[], Any]) -> Any:
                return next_() if removed else listener(fiber, config, next_)

            hooks = ctx.fiber.update_hooks
            remove = hooks.unshift(guarded) if options.prepend else hooks.push(guarded)

            def revert() -> None:
                nonlocal removed
                removed = True
                remove()

            return revert

        return ctx.fiber.effect(setup, 'ctx.on("internal/update")')

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

    def isolated_since(self, name: str, ancestor: "Context") -> bool:
        """Return whether a context between `ancestor` and this one isolates `name`.

        Args:
            name: The service name.
            ancestor: A context that this context derives from, or this context.
        """
        extra = len(self._isolates.maps) - len(ancestor._isolates.maps)
        return any(name in layer for layer in self._isolates.maps[:extra])

    def intercept_layers(self, name: str) -> list[object]:
        """Return the intercept configurations of `name`, from the outermost context in.

        Each context that called `intercept` for `name` adds one entry.
        """
        return [
            layer[name] for layer in reversed(self._intercepts.maps) if name in layer
        ]

    def bind_fiber(self, fiber: Fiber) -> Self:
        """Return a child context whose fiber is `fiber`.

        A `Fiber` calls it for the context that its component receives.
        """
        return self._derive(self._isolates, self._intercepts, self._meta, fiber)

    def declare_realm(self, name: str) -> None:
        """Give the service `name` a default realm in the root context if it has none.

        The default realm is shared by every context that does not isolate `name`. It is
        stored in the root context, so a later `isolate` call hides it.
        """
        self.root._isolates.setdefault(name, object())

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

    def use(self, component: Component, config: object = None) -> Fiber:
        """Run `component` in a new fiber that this context's fiber owns.

        The fiber loads when every service in the component's `inject` has a provider,
        and it ends when this context's fiber ends. Inside a task, the call returns
        before the component runs; await the fiber to wait for the load.

        Args:
            component: A function `(ctx, config)`, a class whose constructor takes
                `(ctx, config)`, or an object with an `apply(ctx, config)` method.
            config: The raw config. The component's `Config` validates it.

        Returns:
            The new fiber. A task can `await` it.

        Raises:
            TypeError: If `component` is not a valid component.
            CordisError: If this context's fiber is disposed.

        Examples:
            ```python
            >>> def greet(ctx, config):
            ...     print("hello", config)
            ...     return lambda: print("bye", config)
            >>> fiber = ctx.use(greet, "world")
            hello world
            >>> _ = fiber.dispose()
            bye world
            ```
        """
        with self.scheduler.call():
            return self.registry.use(self, component, config, CallSite.capture(skip=1))

    def inject(self, deps: object, callback: Callable[["Context"], Any]) -> Fiber:
        """Run `callback(ctx)` in a new fiber that requires the services `deps`.

        Args:
            deps: Service names or keys, or a dict from a name to its intercept
                configuration.
            callback: The function to run. It gets the context of the new fiber and
                can return an effect.

        Returns:
            The new fiber.
        """
        with self.scheduler.call():
            return self.registry.inject(self, deps, callback, CallSite.capture(skip=1))

    def __getitem__(self, key: Keyable[Any]) -> Any:
        """Return the value that the accessor or the service `key` gives.

        An accessor takes precedence over a service of the same name. At the root, the
        access is unchecked: it gives the value of the provider whatever the state of
        the provider's fiber, or `None` when no provider is visible.

        A context that belongs to a component's fiber reads only the services that the
        fiber or one of its ancestors injects or provides.

        Raises:
            CordisError: In a component's context, if no fiber injects the service, or
                if the fiber that injects it is not loaded.
            TypeError: If `key` is neither a `Key` nor a `str`.
        """
        return self.reflect.read(self, key)

    def __setitem__(self, key: Keyable[Any], value: Any) -> None:
        """Set the value of the accessor or the service `key`.

        Raises:
            CordisError: If `key` is an accessor without a setter, or if no service
                `key` is provided.
            TypeError: If `key` is neither a `Key` nor a `str`.
        """
        self.reflect.write(self, key, value)

    def get[T](self, key: Keyable[T], strict: bool = True) -> T | None:
        """Return the value of the service `key`, or `None`.

        Args:
            key: The service key.
            strict: When true, a provider whose fiber is not `ACTIVE` gives `None`.

        Returns:
            The value that the provider holds. `None` when no provider is visible from
            this context, or when `strict` hides the provider.
        """
        return self.reflect.get(self, key, strict)

    def set[T](self, key: Keyable[T], value: T) -> None:
        """Replace the value of the service `key` that this context sees.

        Raises:
            CordisError: If no service `key` is provided.
        """
        self.reflect.set(self, key, value)

    def provide[T](
        self,
        key: Keyable[T],
        value: T | None = None,
        check: Callable[[], bool] | None = None,
    ) -> EffectHandle:
        """Make `value` available as the service `key`.

        The service belongs to this context's fiber: the fiber removes it when it
        unloads. The removal waits until the fibers that depend on the service have
        unloaded. A context whose realm for `key` is isolated has its own providers.

        Args:
            key: The service key.
            value: The service value. Replace it later with `set`.
            check: Called with no argument when a fiber that injects `key` looks for
                its provider. The fiber does not use this provider while the call
                returns a false value or raises.

        Returns:
            The handle of the effect. Call it to remove the service.

        Raises:
            CordisError: If the realm already has a provider of `key`, or if `key`
                is declared as an accessor.

        Examples:
            ```python
            >>> remove = ctx.provide("db", {"rows": 0})
            >>> ctx.get("db")
            {'rows': 0}
            >>> remove()
            ```
        """
        return self.reflect.provide(self, key, value, check)

    def accessor(
        self,
        key: Keyable[Any],
        get: Callable[["Context"], Any],
        set: Callable[["Context", Any], None] | None = None,
    ) -> EffectHandle:
        """Compute the value of `ctx[key]` with `get`.

        Args:
            key: The property name.
            get: Called with the context that is read. It gives the value.
            set: Called with the context and the new value on `ctx[key] = value`.
                Without it, an assignment raises `CordisError`.

        Returns:
            The handle of the effect. Call it to remove the accessor.

        Raises:
            CordisError: If `key` is already declared as a service or an accessor.
        """
        return self.reflect.accessor(self, key, Accessor(get, set))

    def mixin(
        self, source: Keyable[Any], mixins: Iterable[str] | Mapping[str, str]
    ) -> EffectHandle:
        """Expose attributes of the service `source` as properties of the context.

        After the call, `ctx[name]` reads the attribute `name` of `ctx[source]`, and
        `ctx[name] = value` sets it. A mapping gives the attribute name as the key and
        the property name as the value.

        Args:
            source: The key of the service that has the attributes.
            mixins: The attribute names, or a mapping from attribute name to property
                name.

        Returns:
            The handle of the effect. Call it to remove the accessors.

        Raises:
            CordisError: If a property name is already declared.
        """
        return self.reflect.mixin(self, source, mixins)

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
        fiber: Fiber | None = None,
    ) -> Self:
        child = object.__new__(type(self))
        child.scheduler = self.scheduler
        child.root = self.root
        child.fiber = self.fiber if fiber is None else fiber
        child.events = self.events
        child.reflect = self.reflect
        child.registry = self.registry
        child._isolates = isolates
        child._intercepts = intercepts
        child._meta = meta
        return child
