"""Contexts.

A `Context` is the handle through which code registers effects. Derived contexts share
the fiber and the scheduler of their parent, and carry their own isolation, intercept,
and meta tables. Start with `Context(scheduler)` to create a root context.
"""

from collections import ChainMap
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Self

from pycordis.deferred import Deferred
from pycordis.errors import CallSite, CordisError
from pycordis.events import EventName, EventOptions, EventsService, Listener
from pycordis.fiber import EffectForm, EffectHandle, Fiber, resolve_config
from pycordis.keys import Keyable, key_name
from pycordis.logger import LoggerService
from pycordis.reflect import Accessor, ReflectService
from pycordis.registry import Component, RegistryService
from pycordis.scheduler import Scheduler, owner_thread
from pycordis.traceable import get_traceable


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


class InterceptLink(Mapping[str, object]):
    """A layer that reads the intercept configurations of another context.

    `Context.intercept_through` makes a context whose intercept layers are those of
    `target`. The link reads `target` on each lookup, so assigning a new `target`
    changes what that context and every context that derives from it see. A host that
    moves a component from one parent context to another, and keeps the component
    running, assigns the new parent.

    Attributes:
        target: The context whose intercept layers the link gives. Assign it on the
            owner thread.
    """

    def __init__(self, target: "Context") -> None:
        self.target = target

    def __getitem__(self, name: str) -> object:
        return self.target.intercepts[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self.target.intercepts)

    def __len__(self) -> int:
        return len(self.target.intercepts)


@dataclass(frozen=True, slots=True)
class Rule:
    """Which service names one dimension of a `restrict` call admits.

    A `restrict` call builds one `Rule` for inject keys and reads, and one `Rule` for
    provide keys. A name passes the rule when it is in `names`, or starts with one of
    `prefixes`.

    Attributes:
        names: The admitted names, by exact match. A `Key[T]` passed to `restrict`
            becomes its name.
        prefixes: The admitted name prefixes, such as `"plugin."`.
    """

    names: frozenset[str]
    prefixes: tuple[str, ...] = ()

    def admits(self, name: str) -> bool:
        """Return whether `name` passes this rule."""
        return name in self.names or name.startswith(self.prefixes)

    def __str__(self) -> str:
        names = ", ".join(sorted(self.names))
        prefixes = ", ".join(sorted(self.prefixes))
        parts = [
            part
            for part in (
                f"names: {names}" if names else "",
                f"prefixes: {prefixes}" if prefixes else "",
            )
            if part
        ]
        return "; ".join(parts) if parts else "no names"


@dataclass(frozen=True, slots=True)
class Restriction:
    """One `restrict` call's rules, carried in a context's restriction chain.

    A context admits a key only when every `Restriction` in its chain admits it (see
    `restrict`).

    Attributes:
        inject: The rule for inject keys and reads, or `None` to admit every key.
        provide: The rule for provide keys, or `None` to admit every key.
    """

    inject: Rule | None
    provide: Rule | None


class Context:
    """The place from which code registers effects.

    A root context owns the root fiber. `extend`, `isolate`, and `intercept` return new
    contexts that point to their parent: the parent does not change, and a table
    lookup reads the nearest context that has the key.

    Call context methods on the owner thread of the root context, which is the thread
    that created it. A call from another thread raises `CordisError("WRONG_THREAD")`.
    To hand work to the owner thread, use `Scheduler.submit`.

    Examples:
        ```python
        >>> ctx = Context(ImmediateScheduler())
        >>> handle = ctx.effect(lambda: lambda: print("reverted"))
        >>> handle()
        reverted
        ```

    Attributes:
        scheduler: The scheduler that runs the work of the root context.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self._root: Self = self
        self._isolates: ChainMap[str, object] = ChainMap()
        self._intercepts: ChainMap[str, object] = ChainMap()
        self._meta: ChainMap[str, object] = ChainMap()
        self._restrictions: tuple[Restriction, ...] = ()
        self._shadow: Context | None = None
        self._use_site: Context | None = None
        self._events = EventsService(scheduler)
        self._reflect = ReflectService(scheduler)
        self._registry = RegistryService(scheduler)
        self._fiber = Fiber(self)
        self._logger = LoggerService(self)
        self._events.install(
            self,
            "internal/config",
            lambda fiber, next_: resolve_config(fiber.runtime.config, next_()),
        )
        self._events.install(self, "internal/listener", self._route_update_listener)
        self._events.install(self, "internal/update", _run_update_hooks, prepend=True)

    def __repr__(self) -> str:
        return f"Context <{self._fiber.name}>"

    def _check_guarded(self, name: str) -> None:
        """Raise if a restriction on the def site makes `name` unreadable.

        A restricted plugin that merely injects a service cannot reach the root
        context, or the services that store providers, listeners, or components,
        through it: each of those gives an unrestricted view of the whole tree.
        """
        site = self if self._shadow is None else self._shadow
        if site._restrictions:
            raise CordisError(f'cannot read "{name}" in a restricted context')

    @property
    def fiber(self) -> Fiber:
        """The fiber that owns the effects that this context registers.

        Raises:
            CordisError: If `restrict` added a restriction to this context without
                binding it to a new fiber, so that this attribute would give the
                fiber of a less-restricted ancestor. `use`, `inject`, and the other
                methods that create a fiber do not trip this guard.
        """
        if self._restrictions is not self._fiber.ctx._restrictions:
            raise CordisError('cannot read "fiber" in a restricted context')
        return self._fiber

    @property
    def root(self) -> Self:
        """The root context.

        Raises:
            CordisError: In a restricted context.
        """
        self._check_guarded("root")
        return self._root

    @property
    def events(self) -> EventsService:
        """The service that stores the listeners of the root context.

        Every derived context shares it.

        Raises:
            CordisError: In a restricted context.
        """
        self._check_guarded("events")
        return self._events

    @property
    def reflect(self) -> ReflectService:
        """The service that stores the providers and accessors of the root context.

        Every derived context shares it.

        Raises:
            CordisError: In a restricted context.
        """
        self._check_guarded("reflect")
        return self._reflect

    @property
    def registry(self) -> RegistryService:
        """The service that records the loaded components of the root context.

        Every derived context shares it.

        Raises:
            CordisError: In a restricted context.
        """
        self._check_guarded("registry")
        return self._registry

    @property
    def logger(self) -> LoggerService:
        """The logging service of the root context, as this context sees it.

        Call it to get a `Logger`, or log through it directly. A call from a service
        method names the logger after the service. See `LoggerService`.
        """
        return get_traceable(self, self._root._logger)

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

            hooks = ctx._fiber.update_hooks
            remove = hooks.unshift(guarded) if options.prepend else hooks.push(guarded)

            def revert() -> None:
                nonlocal removed
                removed = True
                remove()

            return revert

        return ctx._fiber.effect(setup, 'ctx.on("internal/update")')

    @property
    def shadow(self) -> "Context | None":
        """The def-site context, or `None`.

        A service method runs with a context that acts for the caller (the use site) and
        resolves services as the context of the service (the def site) would. This is
        that def-site context. A context that is not such a context gives `None`.
        """
        return self._shadow

    @property
    def use_site(self) -> Self:
        """This context without its `shadow`.

        The methods that create fibers, providers, and listeners act on it, so that
        nothing registered keeps a def-site link.
        """
        return self if self._use_site is None else self._use_site

    def with_shadow(self, def_site: "Context") -> Self:
        """Return a context that acts for this one and resolves services at `def_site`.

        Only `ctx[key]` reads the def site. Isolation, intercept, effects, and every
        other method are those of this context. A context derived from the result keeps
        the def site.
        """
        use_site = self.use_site
        shadowed = object.__new__(type(self))
        shadowed.__dict__.update(use_site.__dict__)
        shadowed._shadow = def_site
        shadowed._use_site = use_site
        return shadowed

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

        Each context that called `intercept` for `name` adds one entry. A link layer
        (see `intercept_through`) adds the entries of its target.
        """
        values: list[object] = []
        for layer in reversed(self._intercepts.maps):
            if isinstance(layer, InterceptLink):
                values.extend(layer.target.intercept_layers(name))
            elif name in layer:
                values.append(layer[name])
        return values

    def bind_fiber(self, fiber: Fiber) -> Self:
        """Return a child context whose fiber is `fiber`.

        A `Fiber` calls it for the context that its component receives.
        """
        return self.use_site._derive(
            self._isolates, self._intercepts, self._meta, fiber
        )

    def declare_realm(self, name: str) -> None:
        """Give the service `name` a default realm in the root context if it has none.

        The default realm is shared by every context that does not isolate `name`. It is
        stored in the root context, so a later `isolate` call hides it.
        """
        self._root._isolates.setdefault(name, object())

    @owner_thread("scheduler")
    def extend(self, **meta: object) -> Self:
        """Return a child context that adds `meta` to its `meta` mapping.

        A name in `meta` hides the same name in the parent's mapping.
        """
        return self._derive(
            self._isolates, self._intercepts, self._meta.new_child(meta)
        )

    @owner_thread("scheduler")
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

    @owner_thread("scheduler")
    def intercept(self, name: str, config: object) -> Self:
        """Return a child context that passes `config` to the service `name`."""
        return self._derive(
            self._isolates, self._intercepts.new_child({name: config}), self._meta
        )

    @owner_thread("scheduler")
    def intercept_layer(self, layer: Mapping[str, object]) -> Self:
        """Return a child context that passes the configurations in `layer` to services.

        `layer` is a mapping from a service name to its configuration, and it is not
        copied. The child context reads it each time a service resolves its intercept
        configuration, so a change to `layer` is visible to every component that runs
        in this context or in a context that derives from it. Use `intercept` when the
        configuration of one service does not change.

        Args:
            layer: The configurations by service name. The caller owns the mapping and
                changes it on the owner thread.

        Examples:
            ```python
            >>> layer = {"http": {"proxy": "a"}}
            >>> inner = ctx.intercept_layer(layer)
            >>> layer["http"] = {"proxy": "b"}
            >>> inner.intercept_layers("http")
            [{'proxy': 'b'}]
            ```
        """
        return self._derive(
            self._isolates,
            self._intercepts.new_child(layer),
            self._meta,  # type: ignore[arg-type]
        )

    @owner_thread("scheduler")
    def intercept_through(self, link: InterceptLink) -> Self:
        """Return a child context whose intercept layers are those of `link.target`.

        The child has the isolation and `meta` of this context, but not its intercept
        layers: it reads the layers of `link.target` on each lookup. Assign
        `link.target` to give the child, and every context that derives from it, the
        layers of another context.

        Args:
            link: The source of the intercept layers. The caller owns it.

        Examples:
            ```python
            >>> a, b = ctx.intercept("http", "a"), ctx.intercept("http", "b")
            >>> link = InterceptLink(a)
            >>> inner = ctx.intercept_through(link)
            >>> link.target = b
            >>> inner.intercept_layers("http")
            ['b']
            ```
        """
        return self._derive(self._isolates, ChainMap(link), self._meta)  # type: ignore[arg-type]

    @owner_thread("scheduler")
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
            return self._fiber.effect(
                execute, label, call_site=CallSite.capture(skip=1)
            )

    @owner_thread("scheduler")
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
            return self._registry.use(
                self.use_site, component, config, CallSite.capture(skip=1)
            )

    @owner_thread("scheduler")
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
            return self._registry.inject(
                self.use_site, deps, callback, CallSite.capture(skip=1)
            )

    def __getitem__(self, key: Keyable[Any]) -> Any:
        """Return the value that the accessor or the service `key` gives.

        An accessor takes precedence over a service of the same name. At the root, the
        access is unchecked: it gives the value of the provider whatever the state of
        the provider's fiber, or `None` when no provider is visible.

        A context that belongs to a component's fiber reads only the services that the
        fiber or one of its ancestors injects or provides. `get` checks the same rule.

        Raises:
            CordisError: In a component's context, if no fiber injects the service, or
                if the fiber that injects it is not loaded.
            TypeError: If `key` is neither a `Key` nor a `str`.
        """
        return self._reflect.read(self, key)

    def __setitem__(self, key: Keyable[Any], value: Any) -> None:
        """Set the value of the accessor or the service `key`.

        Raises:
            CordisError: If `key` is an accessor without a setter, or if no service
                `key` is provided.
            TypeError: If `key` is neither a `Key` nor a `str`.
        """
        self._reflect.write(self, key, value)

    @owner_thread("scheduler")
    def get[T](self, key: Keyable[T], strict: bool = True) -> T | None:
        """Return the value of the service `key`, or `None`.

        A context that belongs to a component's fiber reads only the services that
        the fiber or one of its ancestors injects or provides, as `ctx[key]` does. The
        read gives the provider that the declaring fiber loaded against: after a
        change of provider, it gives the old provider until the fiber loads again.
        Where `ctx[key]` raises because the declaring fiber is not loaded, `get`
        returns `None`. Accessors are not read. At the root (`root`,
        `root.isolate(...)`), the read is not checked.

        For an optional service, read it in a child fiber that injects it:
        `ctx.inject([key], callback)` runs `callback` when a provider is visible, and
        the component itself does not wait for the service. For a service in another
        realm, use `ctx.isolate(key, label).inject([key], callback)`: an ancestor's
        `inject` does not admit a read across the isolate. A host that wants to see
        which provider a context resolves, without the check, calls
        `ctx.reflect.get(ctx, key, strict)`.

        Args:
            key: The service key.
            strict: When true, a provider whose fiber is not `ACTIVE` gives `None`.

        Returns:
            The value that the provider holds. `None` when the declaring fiber is not
            loaded, when `strict` hides the provider, or, at the root, when no
            provider is visible.

        Raises:
            CordisError: In a component's context, if no fiber injects or provides the
                service. A note on the error names the two ways to declare the read.
            TypeError: If `key` is neither a `Key` nor a `str`.

        Examples:
            ```python
            >>> def apply(ctx, config):
            ...     ctx.inject(["metrics"], lambda inner: print(inner.get("metrics")))
            >>> fiber = root.use(apply)  # loads with no "metrics" provider
            ```
        """
        return self._reflect.checked_get(self, key, strict)

    @owner_thread("scheduler")
    def set[T](self, key: Keyable[T], value: T) -> None:
        """Replace the value of the service `key` that this context sees.

        Raises:
            CordisError: If no service `key` is provided.
        """
        self._reflect.set(self, key, value)

    @owner_thread("scheduler")
    def provide[T](
        self,
        key: Keyable[T],
        value: T | None = None,
        check: Callable[[T], bool] | None = None,
    ) -> EffectHandle:
        """Make `value` available as the service `key`.

        The service belongs to this context's fiber: the fiber removes it when it
        unloads. The removal waits until the fibers that depend on the service have
        unloaded. A context whose realm for `key` is isolated has its own providers.

        Args:
            key: The service key.
            value: The service value. Replace it later with `set`.
            check: Called with the value, as the context of a fiber that injects
                `key` sees it, when that fiber looks for its provider. The fiber does
                not use this provider while the call returns a false value or raises.

        Returns:
            The handle of the effect. Call it to remove the service.

        Raises:
            CordisError: If the realm already has a provider of `key`, or if `key`
                is declared as an accessor.

        Examples:
            ```python
            >>> remove = root.provide("db", {"rows": 0})
            >>> root.get("db")
            {'rows': 0}
            >>> remove()
            ```
        """
        return self._reflect.provide(self.use_site, key, value, check)

    @owner_thread("scheduler")
    def accessor(
        self,
        key: Keyable[Any],
        get: Callable[["Context", Any], Any],
        set: Callable[["Context", Any, Any], None] | None = None,
    ) -> EffectHandle:
        """Compute the value of `ctx[key]` with `get`.

        Args:
            key: The property name.
            get: Called with the context that is read and the receiver. It gives the
                value. The receiver is the traceable view through which an associated
                access came (see `Tracker`), or `None` for `ctx[key]`.
            set: Called with the context, the new value, and the receiver on
                `ctx[key] = value`. Without it, an assignment raises `CordisError`.

        Returns:
            The handle of the effect. Call it to remove the accessor.

        Raises:
            CordisError: If `key` is already declared as a service or an accessor.
        """
        return self._reflect.accessor(self.use_site, key, Accessor(get, set))

    @owner_thread("scheduler")
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
        return self._reflect.mixin(self.use_site, source, mixins)

    @owner_thread("scheduler")
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
            listener: The function to call with the arguments of the event. An
                argument that has a `Tracker` reaches it as its view for this
                context, so its effects belong to this context's fiber.
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
        return self._events.on(self.use_site, name, listener, options)

    @owner_thread("scheduler")
    def once(
        self,
        name: EventName,
        listener: Listener,
        options: EventOptions | bool = False,
    ) -> EffectHandle:
        """Register `listener` for the event `name`, and remove it after its first call.

        The arguments and the result are those of `on`.
        """
        return self._events.once(self.use_site, name, listener, options)

    @owner_thread("scheduler")
    def emit(self, name: EventName, *args: Any, this: object = None) -> None:
        """Call every listener of `name` with `args`, and ignore the results.

        Args:
            name: The event name.
            *args: The arguments for the listeners.
            this: An object with a `cordis_filter(ctx)` method that selects the
                listeners by the context that registered them.
        """
        self._events.emit(name, *args, this=this)

    @owner_thread("scheduler")
    def bail(self, name: EventName, *args: Any, this: object = None) -> Any:
        """Call the listeners in order, and return the first result that is not
        `None` or `False`.

        The arguments are those of `emit`.
        """
        return self._events.bail(name, *args, this=this)

    @owner_thread("scheduler")
    def serial(self, name: EventName, *args: Any, this: object = None) -> Deferred[Any]:
        """Call the listeners one after another, and wait for each result.

        The arguments are those of `emit`. The result is a `Deferred` of the first
        result that is not `None` or `False`.
        """
        return self._events.serial(name, *args, this=this)

    @owner_thread("scheduler")
    def parallel(
        self, name: EventName, *args: Any, this: object = None
    ) -> Deferred[None]:
        """Start every listener, and wait until all of them finish.

        The arguments are those of `emit`. The `Deferred` rejects with an
        `ExceptionGroup` when any listener fails.
        """
        return self._events.parallel(name, *args, this=this)

    @owner_thread("scheduler")
    def waterfall(self, name: EventName, *args: Any, this: object = None) -> Any:
        """Run the listeners as a chain around the final function in `args[-1]`.

        Each listener receives the other arguments and a `next` function that runs the
        rest of the chain. See `EventsService.waterfall`.
        """
        return self._events.waterfall(name, *args, this=this)

    def _derive(
        self,
        isolates: ChainMap[str, object],
        intercepts: ChainMap[str, object],
        meta: ChainMap[str, object],
        fiber: Fiber | None = None,
        *,
        restrictions: tuple[Restriction, ...] | None = None,
    ) -> Self:
        child = object.__new__(type(self))
        child.scheduler = self.scheduler
        child._root = self._root
        child._fiber = self._fiber if fiber is None else fiber
        child._events = self._events
        child._reflect = self._reflect
        child._registry = self._registry
        child._isolates = isolates
        child._intercepts = intercepts
        child._meta = meta
        child._restrictions = (
            self._restrictions if restrictions is None else restrictions
        )
        child._shadow = None
        child._use_site = None
        return child if self._shadow is None else child.with_shadow(self._shadow)

    def _rejecting(self, name: str, *, provide: bool = False) -> Rule | None:
        """Return the first `Rule` in this context's chain that rejects `name`.

        Args:
            name: The service name.
            provide: Check the provide dimension of each `Restriction` instead of the
                inject-and-reads dimension.
        """
        for restriction in self._restrictions:
            rule = restriction.provide if provide else restriction.inject
            if rule is not None and not rule.admits(name):
                return rule
        return None


def _rule(
    names: Iterable[Keyable[Any]] | None,
    prefixes: Iterable[str] | None,
    names_arg: str,
    prefixes_arg: str,
) -> Rule | None:
    """Build the `Rule` of one dimension of `restrict`, or `None` for no rule."""
    if names is None and prefixes is None:
        return None
    if isinstance(names, str):
        raise TypeError(f"{names_arg} must be an iterable of keys, not a bare str")
    if isinstance(prefixes, str):
        raise TypeError(f"{prefixes_arg} must be an iterable of str, not a bare str")
    return Rule(
        frozenset() if names is None else frozenset(key_name(n) for n in names),
        () if prefixes is None else tuple(prefixes),
    )


@owner_thread("scheduler")
def restrict(
    ctx: Context,
    *,
    inject: Iterable[Keyable[Any]] | None = None,
    inject_prefixes: Iterable[str] | None = None,
    provide: Iterable[Keyable[Any]] | None = None,
    provide_prefixes: Iterable[str] | None = None,
) -> Context:
    """Return a context that admits only the given inject, read, and provide keys.

    !!! warning "Guard, not sandbox"
        This restriction stops accidental and undeclared access, and fails fast. It
        does not stop hostile code, which can reach other objects through `gc`,
        `sys._getframe`, or module globals. Run untrusted code in another process.

    !!! warning "Experimental"
        This API can change before `pycordis` exports it.

    The restriction reaches every context that the result derives: a fiber's own
    context, and a context from `use`, `inject`, `isolate`, `intercept`, `extend`, or a
    further `restrict` call. A key is admitted only when every `restrict` call in the
    chain admits it, so a later call can narrow the restriction, but never widen it.

    `inject` and `provide` take exact service names or `Key` objects; a `Key[T]`
    becomes its name. `inject_prefixes` and `provide_prefixes` admit any name that
    starts with one of their strings. Leaving both arguments of a dimension `None`
    admits every key for it; an explicit empty iterable admits none.

    A fiber that a derived context creates fails to load when one of its `inject` keys
    is outside the inject rule: it goes to `FAILED`, and `fiber.error` names the key
    and the rejecting `Rule`. A read (`ctx[key]`, `ctx.get`) outside the inject rule
    raises `CordisError` the same way, checked at the def site of the read (see
    `Context.shadow`): a mixin accessor is checked by the service name it forwards,
    not by its own name. A `ctx.provide`, `ctx.accessor`, or `ctx.mixin` call outside
    the provide rule raises from the call itself, so a component that makes the call
    in its own setup fails to load with no effect installed. `ctx.root`, `ctx.reflect`,
    `ctx.registry`, and `ctx.events` raise `CordisError` on the result, and on every
    context it derives, because each of them gives an unrestricted view of the whole
    tree. `ctx.fiber` raises on the result itself, until a call such as `use` or
    `inject` binds it to a fiber of its own: before that, the attribute would give the
    fiber of the unrestricted or less-restricted context that `ctx` derives from.

    Args:
        ctx: The context to restrict.
        inject: The exact names and keys that a fiber derived from the result can
            declare in `inject`, and that a read (`ctx[key]`, `ctx.get`) can reach.
            `None` together with `inject_prefixes` admits every key.
        inject_prefixes: The name prefixes for the same dimension as `inject`.
        provide: The exact names and keys that `ctx.provide`, `ctx.accessor`, and
            `ctx.mixin` can register. `None` together with `provide_prefixes` admits
            every key.
        provide_prefixes: The name prefixes for the same dimension as `provide`.

    Returns:
        A context derived from `ctx` that carries the restriction.

    Raises:
        ValueError: If `inject`, `inject_prefixes`, `provide`, and `provide_prefixes`
            are all `None`.
        TypeError: If one of the four arguments is a bare `str` in place of an
            iterable: its characters would become names.

    Examples:
        ```python
        >>> plugin_ctx = restrict(root, inject=["db"], provide_prefixes=("plugin.",))
        >>> fiber = plugin_ctx.use(a_plugin)
        ```
    """
    if (
        inject is None
        and inject_prefixes is None
        and provide is None
        and provide_prefixes is None
    ):
        raise ValueError(
            "restrict needs at least one of inject, inject_prefixes, provide, "
            "provide_prefixes"
        )
    restriction = Restriction(
        _rule(inject, inject_prefixes, "inject", "inject_prefixes"),
        _rule(provide, provide_prefixes, "provide", "provide_prefixes"),
    )
    return ctx._derive(
        ctx._isolates,
        ctx._intercepts,
        ctx._meta,
        restrictions=(*ctx._restrictions, restriction),
    )
