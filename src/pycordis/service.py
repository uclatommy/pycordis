"""The `Service` base class.

A service is an object that a component provides to other components under a name.
Subclass `Service` and call its constructor with the context of the component. The
constructor provides the object under the service name, so a class that has `Service` as
its base works as a component and as a service in one declaration. Start with `Service`.
"""

from typing import TYPE_CHECKING, Any, ClassVar

from pycordis.reflect import locate

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
      the hook returns a true value.
    - `cordis_merge_intercept(self, *values) -> T`: Combines the intercept
      configurations that apply to the service. The default merges the values as dicts,
      so a later value replaces the same key of an earlier value.

    Attributes:
        provide: The service name that a subclass declares. The constructor uses it
            when the caller gives no name.
        ctx: The context of the component that provides the service.
        name: The service name.

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
        >>> fiber = ctx.use(Database)
        >>> ctx.get("db")
        <Database object>
        ```
    """

    provide: ClassVar[str | None] = None

    def __init__(self, ctx: "Context", name: str | None = None) -> None:
        name = self.provide if name is None else name
        if name is None:
            raise TypeError(
                f"{type(self).__name__} needs a service name: "
                "pass `name` or set `provide`"
            )
        self.ctx = ctx
        self.name = name
        ctx.provide(name, self, getattr(self, "cordis_check", None))

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

    def cordis_resolve_intercept(self, caller: "Context | None" = None) -> T:
        """Return the configuration that applies to this service for one caller.

        The result merges, from the outermost to the innermost:

        1. the `inject` configuration of the fiber that admits the access, which is the
           nearest fiber that declares this service in `inject`;
        2. each `Context.intercept` configuration of the caller's context chain.

        A fiber that provides the service itself adds no `inject` configuration.

        Args:
            caller: The context that uses the service. The default is the context of
                the service.
        """
        site = self.ctx if caller is None else caller
        values = site.intercept_layers(self.name)
        found = locate(site, self.name)
        if found is not None and found.fiber is not None:
            config = found.fiber.inject[self.name]
            if config is not None:
                values.insert(0, config)
        return self.cordis_merge_intercept(*values)
