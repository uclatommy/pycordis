"""Services and accessors.

A service is a named value that one fiber provides and other code reads through a
context. This module implements the lookup rules: which provider a context sees (its
realm), when a provider is visible, and which fiber may change the value. Start with
`Context.provide`, `Context.get`, and `Context[key]`; `ReflectService` is the object
that stores the providers.

A realm is the identity that groups the contexts that share one provider of a service
name. `Context.isolate` gives a context its own realm for a name.
"""

import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import EffectHandle, FiberState
from pycordis.keys import Keyable, key_name
from pycordis.scheduler import Scheduler, start

if TYPE_CHECKING:
    from pycordis.context import Context


@dataclass(frozen=True, slots=True)
class Accessor:
    """A property that a function computes.

    Attributes:
        get: Called with the context that is read. It gives the value.
        set: Called with the context and the new value, or `None` for a read-only
            property.
    """

    get: Callable[["Context"], Any]
    set: Callable[["Context", Any], None] | None = None


@dataclass(eq=False, slots=True)
class Impl:
    """One provider of a service.

    Attributes:
        uid: The number of the provider. A new provision gets a new number.
        name: The service name.
        ctx: The context that provides the service.
        value: The service value.
        isolate: The realm of the provider.
        dependents: The fibers that read the service. The provider's removal waits
            until they leave.
        release: Called when the last dependent leaves, while a removal waits.
    """

    uid: int
    name: str
    ctx: "Context"
    value: Any
    isolate: object
    dependents: set[Any] = field(default_factory=set)
    release: Callable[[], None] | None = None


class _Filter:
    """The `this` object of an `internal/service` event.

    It selects the listeners whose context uses the same realm of the service as the
    context that provides it.
    """

    def __init__(self, name: str, realm: object | None) -> None:
        self._name = name
        self._realm = realm

    def cordis_filter(self, ctx: "Context") -> bool:
        return ctx.isolates.get(self._name) is self._realm


class ReflectService:
    """Stores the providers and the accessors of a root context.

    Use the methods of `Context`. Every method takes the context that makes the call:
    its realms select the provider, and its fiber owns the effects that the call
    registers.

    Attributes:
        scheduler: The scheduler that runs the waits of provider removals.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self.scheduler = scheduler
        self._store: dict[object, Impl] = {}
        self._services: set[str] = set()
        self._accessors: dict[str, Accessor] = {}
        self._impl_counter = 0

    def read(self, ctx: "Context", key: Keyable[Any]) -> Any:
        """Return `ctx[key]`: an accessor's value, or the value of a provider."""
        name = key_name(key)
        accessor = self._accessors.get(name)
        if accessor is not None:
            return accessor.get(ctx)
        return self.get(ctx, name, False)

    def write(self, ctx: "Context", key: Keyable[Any], value: Any) -> None:
        """Perform `ctx[key] = value`: call an accessor's setter, or set a service."""
        name = key_name(key)
        accessor = self._accessors.get(name)
        if accessor is None:
            self.set(ctx, name, value)
        elif accessor.set is None:
            raise CordisError(f'property "{name}" is read-only')
        else:
            accessor.set(ctx, value)

    def get(self, ctx: "Context", key: Keyable[Any], strict: bool = True) -> Any:
        """Return the value of the service `key` that `ctx` sees, or `None`."""
        impl = self._impl(ctx, key_name(key), strict)
        return None if impl is None else impl.value

    def set(self, ctx: "Context", key: Keyable[Any], value: Any) -> None:
        """Replace the value of the service `key` that `ctx` sees.

        Raises:
            CordisError: If no provider is visible.
        """
        name = key_name(key)
        impl = self._impl(ctx, name, False)
        if impl is None:
            raise CordisError(f'cannot set property "{name}" without provide')
        impl.value = value

    def provide(
        self, ctx: "Context", key: Keyable[Any], value: Any = None
    ) -> EffectHandle:
        """Register `value` as the service `key` on the fiber of `ctx`.

        Returns:
            The handle of the effect. Its removal waits until every dependent of the
            service has left.

        Raises:
            CordisError: If the realm already has a provider, or `key` is an accessor.
        """
        name = key_name(key)
        fiber = ctx.fiber

        def setup() -> Callable[[], Deferred[None]]:
            if name in self._accessors:
                raise CordisError(f'property "{name}" is already declared as accessor')
            self._services.add(name)
            ctx.declare_realm(name)
            realm = ctx.isolates[name]
            taken = self._store.get(realm)
            if taken is not None:
                raise CordisError(
                    f'service "{name}" has been registered at <{taken.ctx.fiber.name}>'
                )
            self._impl_counter += 1
            impl = Impl(self._impl_counter, name, ctx, value, realm)
            self._store[realm] = impl
            fiber.provisions.add(impl)
            if fiber.state == FiberState.ACTIVE:
                self.notify(ctx, [name])
            return lambda: self._remove(impl)

        return fiber.effect(setup, f"ctx.provide({json.dumps(name)})")

    def notify(self, ctx: "Context", names: Iterable[str]) -> None:
        """Report a change of the providers of `names` to `internal/service` listeners.

        A listener hears the event only when its context uses the same realm of the
        name as `ctx`.
        """
        for name in names:
            impl = self._impl(ctx, name, False)
            ctx.events.emit(
                "internal/service",
                name,
                None if impl is None else impl.value,
                this=_Filter(name, ctx.isolates.get(name)),
            )

    def accessor(
        self, ctx: "Context", key: Keyable[Any], accessor: Accessor
    ) -> EffectHandle:
        """Register `accessor` as the property `key` on the fiber of `ctx`.

        Raises:
            CordisError: If `key` is already declared as a service or an accessor.
        """
        name = key_name(key)

        def setup() -> Callable[[], None]:
            if name in self._services:
                raise CordisError(f'property "{name}" is already declared as service')
            if name in self._accessors:
                raise CordisError(f'property "{name}" is already declared as accessor')
            self._accessors[name] = accessor

            def remove() -> None:
                del self._accessors[name]

            return remove

        return ctx.fiber.effect(setup, f"ctx.accessor({json.dumps(name)})")

    def mixin(
        self,
        ctx: "Context",
        source: Keyable[Any],
        mixins: Iterable[str] | Mapping[str, str],
    ) -> EffectHandle:
        """Register an accessor for each attribute of the service `source`.

        An accessor reads or sets the attribute on the service value that the reading
        context sees. It gives `None` while no provider is visible.
        """
        source_name = key_name(source)
        entries = (
            mixins.items() if isinstance(mixins, Mapping) else ((m, m) for m in mixins)
        )

        def setup() -> Iterator[EffectHandle]:
            for attribute, name in list(entries):
                yield self.accessor(ctx, name, self._forward(source_name, attribute))

        return ctx.fiber.effect(setup, f"ctx.mixin({json.dumps(source_name)})")

    def _impl(self, ctx: "Context", name: str, strict: bool) -> Impl | None:
        realm = ctx.isolates.get(name)
        impl = None if realm is None else self._store.get(realm)
        if impl is None:
            return None
        if strict and impl.ctx.fiber.state != FiberState.ACTIVE:
            return None
        return impl

    def _remove(self, impl: Impl) -> Deferred[None]:
        """Unpublish `impl`, then wait until its dependents leave."""
        ctx = impl.ctx
        if self._store.get(impl.isolate) is impl:
            del self._store[impl.isolate]
        self.notify(ctx.isolate(impl.name, impl.isolate), [impl.name])
        with self.scheduler.call():
            return start(self.scheduler, self._release(impl))

    async def _release(self, impl: Impl) -> None:
        # Every removal waits at least one step, so that the unload of a child can still
        # read the service before the provision is dropped.
        gate = Deferred[None]()
        impl.release = lambda: gate.resolve(None)
        if not impl.dependents:
            gate.resolve(None)
        await gate
        impl.release = None
        impl.ctx.fiber.provisions.discard(impl)

    def _forward(self, source: str, attribute: str) -> Accessor:
        def get(ctx: "Context") -> Any:
            service = self.read(ctx, source)
            return None if service is None else getattr(service, attribute)

        def set_(ctx: "Context", value: Any) -> None:
            setattr(self.read(ctx, source), attribute, value)

        return Accessor(get, set_)
