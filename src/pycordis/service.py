"""The `Service` base class.

A service is an object that a component provides to other components under a name.
Subclass `Service` and call its constructor with the context of the component. The
constructor provides the object under the service name, so a class that has `Service` as
its base works as a component and as a service in one declaration. Start with `Service`.
"""

from typing import TYPE_CHECKING, Any, ClassVar, Self

from pycordis.reflect import locate
from pycordis.traceable import Tracker, get_original, get_traceable

if TYPE_CHECKING:
    from pycordis.context import Context


class Service[T]:
    """A component class that provides itself as a named service.

    The constructor calls `ctx.provide(name, self)`. When a fiber loads the class as a
    component, the fiber then runs `cordis_init` if the subclass defines it, and a
    fiber that injects the name loads after the service is provided. A subclass can
    define these hooks:

    - `cordis_init(self)`: Registers the effects of the service. It can return any
      form that `Context.effect` accepts.
    - `cordis_check(self) -> bool`: A fiber that injects the service uses it only while
      the hook returns a true value. The hook runs on the view for that fiber's
      context, so `self.ctx` is the context of the fiber that injects the service.
    - `cordis_merge_intercept(self, *values) -> T`: Combines the intercept
      configurations that apply to the service. The default merges the values as dicts,
      so a later value replaces the same key of an earlier value.
    - `cordis_invoke(self, *args, **kwargs)`: Makes the service callable. A call runs
      the hook with the use-site context as `self.ctx`.

    A component's context reads the service only when its fiber or an ancestor fiber
    injects it (see `Context.get`). A read through a context (`ctx["name"]`,
    `ctx.get("name")`) gives a view of the service for that context (see
    `pycordis.traceable`): `isinstance` works on it, `view.ctx` is the context that
    reads, and an effect that a method
    registers belongs to the fiber of that context. Attributes that the view cannot
    find on the service are read from the service `"<name>.<attribute>"` and from the
    accessor of that name, so `ctx["foo"].bar` can read the service `foo.bar`.

    Attributes:
        provide: The service name that a subclass declares. The constructor uses it
            when the caller gives no name.
        ctx: The context of the component that provides the service.
        name: The service name.
        cordis_caller: The def-site context of the view that the object is. `None` on
            the service itself.

    Raises:
        TypeError: If neither the constructor argument nor `provide` gives a name.
        CordisError: If the realm of the name already has a provider.

    Examples:
        ```python
        >>> class Database(Service):
        ...     provide = "db"
        ...
        ...     def __init__(self, ctx, config):
        ...         super().__init__(ctx)
        >>> fiber = root.use(Database)
        >>> root.get("db")
        <Database object>
        ```
    """

    provide: ClassVar[str | None] = None
    cordis_caller: ClassVar["Context | None"] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "cordis_invoke" in cls.__dict__:
            cls.__call__ = _invoke_through_view  # type: ignore[method-assign]

    def __init__(self, ctx: "Context", name: str | None = None) -> None:
        name = self.provide if name is None else name
        if name is None:
            raise TypeError(
                f"{type(self).__name__} needs a service name: "
                "pass `name` or set `provide`"
            )
        self.ctx = ctx
        self.name = name
        self.cordis_tracker = Tracker(property="ctx", associate=name)
        # unbound, so that the hook runs with the injecting fiber's view as `self`
        ctx.provide(name, self, getattr(type(self), "cordis_check", None))

    def __getattr__(self, name: str) -> Any:
        # Called only when `name` is missing. An extension reads what it does not
        # override from the service that it extends.
        parent = self.__dict__.get("_cordis_parent")
        if parent is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )
        return getattr(parent, name)

    def cordis_extend(self, **props: Any) -> Self:
        """Return a new service of the same class that extends this one.

        The new object is not provided under the name. It reads every attribute that
        it does not set from this service, also when this service changes later. Its
        `ctx` is the context that this call sees, so a call through a view gives an
        extension for the use site of the view.

        Args:
            **props: The attributes that the extension sets.
        """
        original = get_original(self)
        child = object.__new__(type(original))
        child.__dict__["_cordis_parent"] = original
        child.ctx = self.ctx
        for name, value in props.items():
            setattr(child, name, value)
        return child

    def cordis_filter(self, ctx: "Context") -> bool:
        """Return whether `ctx` uses the realm of this service.

        An `emit` that passes a service as `this` calls only the listeners of contexts
        that use the same provider of the service name.
        """
        return ctx.isolates.get(self.name) is self.ctx.isolates.get(self.name)

    def cordis_merge_intercept(self, *values: Any) -> T:
        """Combine intercept configurations, from the outermost to the innermost.

        The default treats each value as a dict. Override it for another
        configuration type.
        """
        merged: dict[str, Any] = {}
        for value in values:
            merged.update(value)
        return merged  # type: ignore[return-value]

    def cordis_resolve_intercept(self) -> T:
        """Return the configuration that applies to this service for one caller.

        Call it on the view of the service, from a method. The result merges, from the
        outermost to the innermost:

        1. the `inject` configuration of the fiber that admits the access, which is the
           nearest fiber that declares this service in `inject`. The caller is the
           def-site context of the view (`cordis_caller`);
        2. each `Context.intercept` configuration of `self.ctx`, which is the use-site
           context.

        A fiber that provides the service itself adds no `inject` configuration. When
        the object is not a view, `self.ctx` is also the caller.
        """
        return resolve_intercept(self, self.ctx, self.cordis_caller)


def resolve_intercept[T](
    service: Service[T], use_site: "Context", caller: "Context | None"
) -> T:
    """Return the configuration of `service` for the fiber that reads it.

    Only the loader service uses this function: its own context is not the use site, so
    its `cordis_resolve_intercept` calls it. The function is not a public API.

    The result merges, from the outermost to the innermost, the `inject` configuration
    of the fiber that admits the access, and each `Context.intercept` configuration of
    `use_site`.

    Args:
        service: The service.
        use_site: The context whose intercept layers apply.
        caller: The context through which the service is read, or `None` for `use_site`.
    """
    values = use_site.intercept_layers(service.name)
    found = locate(use_site if caller is None else caller, service.name)
    if found is not None and found.fiber is not None:
        config = found.fiber.inject[service.name]
        if config is not None:
            values.insert(0, config)
    return service.cordis_merge_intercept(*values)


def _invoke_through_view(self: Service[Any], *args: Any, **kwargs: Any) -> Any:
    """Call a service that defines `cordis_invoke` through a view of its context."""
    return get_traceable(self.ctx, self)(*args, **kwargs)
