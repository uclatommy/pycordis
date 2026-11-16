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

from collections.abc import Callable, ItemsView, Iterable, KeysView, ValuesView
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

from pycordis.disposables import DisposableList
from pycordis.errors import CallSite
from pycordis.fiber import Fiber
from pycordis.keys import key_name

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

    def __init__(self) -> None:
        self._counter = 0
        self._internal: dict[Callable[..., Any], Runtime] = {}

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

    def get(self, component: Component) -> Runtime | None:
        """Return the record of `component`, or `None` if it has no live fiber."""
        callback = self.resolve(component)
        return None if callback is None else self._internal.get(callback)

    def has(self, component: Component) -> bool:
        """Return whether `component` has a live fiber."""
        return self.get(component) is not None

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
            component: The component to run.
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
        callback = self.resolve(component)
        if callback is None:
            raise _invalid(component)
        ctx.fiber.assert_active()
        runtime = self._internal.get(callback)
        if runtime is None:
            runtime = Runtime(
                _name_of(component),
                callback,
                DisposableList[Fiber](),
                getattr(component, "Config", None),
            )
            self._internal[callback] = runtime
        inject = resolve_inject(getattr(component, "inject", None))
        return Fiber(ctx, config, inject, runtime, call_site)
