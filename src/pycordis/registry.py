"""Components and the registry that runs them.

A component is the unit that a context loads with `Context.use`: a function, a class,
or an object with an `apply` method. Each component runs in its own fiber. A component
declares the services that it needs in its `inject` attribute, and the fiber loads when
every one of them has a provider. Start with `Component` and `Context.use`;
`RegistryService` is the object that records the loaded components.

A component can have these attributes. Set them directly on the function, the class, or
the object:

- `name`: The name that the fiber reports. A function or class without it uses its
  `__name__`.
- `Config`: An object with a `model_validate` method, or a function from the raw config
  to the validated config.
- `inject`: A list of service names or keys, or a dict from a name to the intercept
  configuration of the service.
"""

import inspect
from collections.abc import Callable, ItemsView, Iterable, KeysView, ValuesView
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

from pycordis.disposables import DisposableList
from pycordis.errors import CallSite
from pycordis.fiber import Fiber
from pycordis.keys import key_name
from pycordis.scheduler import Scheduler
from pycordis.traceable import get_traceable

if TYPE_CHECKING:
    from pycordis.context import Context


class Applicable(Protocol):
    """An object that a context can load: it has an `apply` method."""

    def apply(self, ctx: "Context", config: Any) -> Any:
        """Run the component with its context and config."""
        ...


type Component = Callable[..., Any] | Applicable
"""What `Context.use` accepts.

A function `(ctx, config)`, a class whose constructor takes `(ctx, config)`, or an
object with an `apply(ctx, config)` method.
"""

_UNNAMED = ("apply", "<lambda>")


@dataclass(eq=False, slots=True)
class Runtime:
    """The record that all fibers of one component share.

    Attributes:
        name: The name of the component, or `None`.
        callback: The function or class that a fiber runs, or the `apply` method of an
            object. The registry identifies a component by it.
        fibers: The live fibers of the component, in the order of their creation.
        config: The `Config` of the component, or `None`.
    """

    name: str | None
    callback: Callable[..., Any]
    fibers: DisposableList[Fiber]
    config: object | None


def resolve_inject(inject: object) -> dict[str, object]:
    """Return the `inject` attribute of a component as a dict from name to config.

    Args:
        inject: `None`, an iterable of service names or keys, or a mapping from a name
            or key to the intercept configuration.

    Returns:
        The names in the order of `inject`. The value is the intercept configuration,
        or `None`. The order is the order of the epoch of a fiber.
    """
    if inject is None:
        return {}
    if isinstance(inject, dict):
        return {key_name(name): config for name, config in inject.items()}
    if isinstance(inject, str) or not isinstance(inject, Iterable):
        raise TypeError("inject must be a list of names or a dict")
    return {key_name(name): None for name in inject}


def inject(name: str, config: object = None) -> Callable[[Any], Any]:
    """Declare that a component class or one of its methods needs the service `name`.

    On a class, the decorator adds `name` to the `inject` attribute of the class. A
    subclass starts from the `inject` of its base class and never changes it. On a
    method, the method runs in a fiber of its own that loads when `name` has a provider
    and unloads before it loses its provider. The fiber starts when the component is
    created, and the method runs when the fiber loads. The method can return an effect.
    Decorators stack.

    Args:
        name: The service name.
        config: The intercept configuration of the service, or `None`.

    Returns:
        A decorator that returns the class or the function that it gets.

    Raises:
        TypeError: When the decorator is applied to anything but a class or a function.

    Examples:
        ```python
        >>> class Cache(Service):
        ...     @inject("db")
        ...     def warm(self):
        ...         return lambda: print("cooled")
        ```
    """

    def decorate(value: Any) -> Any:
        if inspect.isclass(value):
            inherited = resolve_inject(getattr(value, "inject", None))
            value.inject = {**inherited, name: config}
        elif inspect.isfunction(value):
            deps = getattr(value, "cordis_inject", None)  # set by an earlier decorator
            value.cordis_inject = {**(deps or {}), name: config}
        else:
            raise TypeError("@Inject() can only be used on class or class methods")
        return value

    return decorate


def _call_method(instance: object, name: str, ctx: "Context") -> Any:
    return getattr(get_traceable(ctx, instance), name)()


def _invalid(component: object) -> TypeError:
    return TypeError(
        'invalid component, expect function or object with an "apply" method, '
        f"received {type(component).__name__}"
    )


def _name_of(component: object) -> str | None:
    name = getattr(component, "name", None)
    if not isinstance(name, str):
        name = getattr(component, "__name__", None) if callable(component) else None
    return None if name in _UNNAMED or not isinstance(name, str) else name


class RegistryService:
    """Records the components of a root context and runs them in fibers.

    Use `Context.use` and `Context.inject` to load a component. The registry keeps one
    `Runtime` for each component that has a live fiber, and drops it with its last
    fiber.

    Examples:
        ```python
        >>> def greet(ctx, config):
        ...     print("hello", config)
        >>> fiber = ctx.use(greet, "world")
        hello world
        >>> len(ctx.registry)
        1
        ```
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler
        self._counter = 0
        self._internal: dict[Callable[..., Any], Runtime] = {}
        # The runner of a replaced component, and the component that took its place.
        # The table holds the old runners strongly: a bound `apply` method has no
        # lifetime of its own that a weak reference could follow.
        self._aliases: dict[Callable[..., Any], Component] = {}

    def __len__(self) -> int:
        return len(self._internal)

    def next_uid(self) -> int:
        """Return the number for the next fiber."""
        self._counter += 1
        return self._counter

    def resolve(self, component: Component) -> Callable[..., Any] | None:
        """Return the function that runs `component`, or `None` if it has none.

        A function or a class is its own runner. An object with a callable `apply`
        attribute runs through that attribute.
        """
        try:
            if callable(component):
                return component
            apply = getattr(component, "apply", None)
            return apply if callable(apply) else None
        except Exception:
            # a property of the component raised: the component is not valid
            return None

    def _forward(
        self, component: Component
    ) -> tuple[Callable[..., Any], Component] | None:
        """Follow the replacements of `component` to the component that runs now.

        Returns:
            The runner and the component that it belongs to, or `None` if `component`
            is not valid.
        """
        key = self.resolve(component)
        while key is not None:
            successor = self._aliases.get(key)
            if successor is None:
                return key, component
            component = successor
            key = self.resolve(successor)
        return None

    def _create(self, callback: Callable[..., Any], component: Component) -> Runtime:
        runtime = Runtime(
            _name_of(component),
            callback,
            DisposableList[Fiber](),
            getattr(component, "Config", None),
        )
        self._internal[callback] = runtime
        return runtime

    def get(self, component: Component) -> Runtime | None:
        """Return the record of `component`, or `None` if it has no live fiber.

        A component that `replace` has replaced gives the record of its replacement.
        """
        forwarded = self._forward(component)
        return None if forwarded is None else self._internal.get(forwarded[0])

    def has(self, component: Component) -> bool:
        """Return whether `component`, or its replacement, has a live fiber."""
        return self.get(component) is not None

    def replace(self, old: Component, new: Component) -> list[Fiber]:
        """Move the live fibers of `old` to `new`, and run them again.

        Each moved fiber keeps its identity, its config, and its place in the fiber
        tree. It takes the `inject` of `new`, validates its config with the `Config`
        of `new`, and runs the effects of `old` back before it loads `new`. A failed
        fiber loads again. The `internal/replace` event fires for each fiber, after the
        fiber has its new record. If `new` has live fibers, the moved fibers join them.

        Afterwards `get`, `has`, and `Context.use` treat `old` as `new`, also when
        `old` had no fiber. Replacements chain: the `old` of a later call can be any
        component of an earlier call, and `old` is its own component again when it is
        the `new` of a later call.

        Args:
            old: The component to replace. A replaced component stands for its
                replacement.
            new: The component that takes its place.

        Returns:
            The fibers that moved, in the order of their creation. The list is empty
            when `old` has no live fiber. Call `Fiber.wait` on a fiber to wait for its
            new load.

        Raises:
            TypeError: If `old` or `new` is neither a function, a class, nor an object
                with an `apply` method.

        Examples:
            ```python
            >>> fiber = ctx.use(old_component)
            >>> _ = ctx.registry.replace(old_component, new_component)
            >>> fiber.runtime is ctx.registry.get(new_component)
            True
            ```
        """
        source = self._forward(old)
        if source is None:
            raise _invalid(old)
        key = self.resolve(new)
        if key is None:
            raise _invalid(new)
        with self._scheduler.call():
            # `new` is a component of its own again
            self._aliases.pop(key, None)
            old_key = source[0]
            runtime = self._internal.get(old_key)
            target = self._internal.get(key)
            if old_key != key:
                self._internal.pop(old_key, None)
                self._aliases[old_key] = new
            fibers = [] if runtime is None else list(runtime.fibers)
            if not fibers:
                return fibers
            if target is None:
                target = self._create(key, new)
            inject = resolve_inject(getattr(new, "inject", None))
            for fiber in fibers:
                fiber._replace(target, inject)
            return fibers

    def keys(self) -> KeysView[Callable[..., Any]]:
        """Return a view of the functions that run the loaded components."""
        return self._internal.keys()

    def values(self) -> ValuesView[Runtime]:
        """Return a view of the records of the loaded components."""
        return self._internal.values()

    def entries(self) -> ItemsView[Callable[..., Any], Runtime]:
        """Return a view of the pairs of runner and record."""
        return self._internal.items()

    def for_each(self, callback: Callable[[Runtime, Callable[..., Any]], None]) -> None:
        """Call `callback(runtime, key)` for each loaded component."""
        for key, runtime in list(self._internal.items()):
            callback(runtime, key)

    def unregister(self, runtime: Runtime) -> None:
        """Drop the record of a component that has no fiber."""
        del self._internal[runtime.callback]

    def run_inject_methods(self, ctx: "Context", instance: object) -> None:
        """Start a fiber for each method of `instance` that `inject` decorates.

        The methods run in the order of their definition, the methods of a base class
        first. A method that a subclass overrides without the decorator does not run.
        Each method runs with `instance` as seen from the context of its own fiber.
        """
        members: dict[str, Any] = {}
        for klass in reversed(type(instance).__mro__):
            members.update(vars(klass))
        for member, function in members.items():
            deps = getattr(function, "cordis_inject", None)  # any class attribute
            if deps is not None:
                ctx.inject(deps, partial(_call_method, instance, member))

    def inject(
        self,
        ctx: "Context",
        deps: object,
        callback: Callable[["Context"], Any],
        call_site: CallSite | None = None,
    ) -> Fiber:
        """Run `callback(ctx)` in a new fiber that requires the services `deps`.

        Args:
            ctx: The context that owns the new fiber.
            deps: The `inject` attribute that the component would have.
            callback: The function to run. It gets the context of the new fiber.
            call_site: Where the call came from.

        Returns:
            The new fiber.
        """
        component = SimpleNamespace(
            inject=deps,
            apply=lambda ctx, config: callback(ctx),
            name=getattr(callback, "__name__", None),
        )
        return self.use(ctx, component, None, call_site)

    def use(
        self,
        ctx: "Context",
        component: Component,
        config: object = None,
        call_site: CallSite | None = None,
    ) -> Fiber:
        """Run `component` in a new fiber that `ctx`'s fiber owns.

        Args:
            ctx: The context that owns the new fiber.
            component: The component to run. A component that `replace` has replaced
                runs as its replacement.
            config: The raw config. The component's `Config` validates it when the
                fiber loads.
            call_site: Where the call came from, for the note of a failed load.

        Returns:
            The new fiber. It loads as soon as every service in its `inject` has a
            provider.

        Raises:
            TypeError: If `component` is neither a function, a class, nor an object
                with an `apply` method.
            CordisError: If the fiber of `ctx` is disposed.
        """
        forwarded = self._forward(component)
        if forwarded is None:
            raise _invalid(component)
        ctx.fiber.assert_active()
        callback, target = forwarded
        runtime = self._internal.get(callback)
        if runtime is None:
            runtime = self._create(callback, target)
        inject = resolve_inject(getattr(target, "inject", None))
        return Fiber(ctx, config, inject, runtime, call_site)
