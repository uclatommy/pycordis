"""Services and accessors.

A service is a named value that one fiber provides and other code reads through a
context. This module implements the lookup rules: which provider a context sees (its
realm), when a provider is visible, and which fiber may change the value. Start with
`Context.provide`, `Context.get`, and `Context[key]`; `ReflectService` is the object
that stores the providers.

A realm is the identity that groups the contexts that share one provider of a service
name. `Context.isolate` gives a context its own realm for a name.
"""

import functools
import json
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import EffectHandle, Fiber, FiberState
from pycordis.keys import Keyable, key_name
from pycordis.scheduler import Scheduler, owner_thread, start
from pycordis.traceable import Hidden, Overlay, expose, get_traceable, get_with_receiver

if TYPE_CHECKING:
    from pycordis.context import Context


@dataclass(frozen=True, slots=True)
class Accessor:
    """A property that a function computes.

    Attributes:
        get: Called with the context that is read and the receiver. It gives the value.
            The receiver is the traceable view through which an associated access
            came, or `None` for a plain `ctx[key]`.
        set: Called with the context, the new value, and the receiver, or `None` for a
            read-only property.
        source: The service name that a mixin forwards, or `None` for a plain
            accessor. A read of the accessor is checked against this name instead of
            the accessor's own name, so a restriction that admits `"timer"` but not
            `"sleep"` still rejects `ctx["sleep"]` when `"sleep"` is a mixin of
            `"timer"`.
    """

    get: Callable[["Context", Any], Any]
    set: Callable[["Context", Any, Any], None] | None = None
    source: str | None = None


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
        check: Called with the value, as the view for the context of a fiber that
            injects the service, when that fiber looks for its provider. The fiber does
            not use the provider when the call returns a false value or raises.
    """

    uid: int
    name: str
    ctx: "Context"
    value: Any
    isolate: object
    dependents: set[Any] = field(default_factory=set)
    release: Callable[[], None] | None = None
    check: Callable[[Any], bool] | None = None

    def cordis_expose(self, receiver: "Context") -> Hidden:
        """Return a `Hidden`: a restricted scope never sees a provider record."""
        return Hidden("Impl")


@dataclass(frozen=True, slots=True)
class Located:
    """The result of `locate`: a fiber admits the access.

    Attributes:
        impl: The provider that the access reaches, or `None` when the fiber that
            declares the service is not loaded.
        fiber: The fiber that declares the service in `inject`, or `None` when the
            fiber provides the service itself.
    """

    impl: Impl | None
    fiber: Fiber | None = None


def locate(ctx: "Context", name: str) -> Located | None:
    """Find which fiber admits an access to the service `name` from `ctx`.

    The walk starts at the fiber of `ctx` and goes to its ancestors. A fiber admits the
    access when it provides `name` in the realm of `ctx`, or when it declares `name` in
    `inject`. The walk stops at a context that isolates `name` again.

    Returns:
        The provider, or `None` when no fiber admits the access.
    """
    realm = ctx.isolates.get(name)
    fiber = ctx._fiber
    while True:
        for provision in fiber.provisions:
            if provision.name == name and provision.ctx.isolates.get(name) is realm:
                return Located(provision)
        if ctx.isolated_since(name, fiber.ctx):
            return None
        impl = None if fiber.committed is None else fiber.committed.get(name)
        if impl is not None or name in fiber.inject:
            return Located(impl, fiber)
        if fiber.runtime is None:
            return None
        fiber = fiber.parent._fiber


def _visible(impl: Impl | None, strict: bool) -> Impl | None:
    """Return `impl`, or `None` when `strict` hides it: its fiber is not `ACTIVE`."""
    if impl is None or (strict and impl.ctx._fiber.state != FiberState.ACTIVE):
        return None
    return impl


def _already_declared(name: str, declared: str) -> CordisError:
    """Return the error for a `name` that is already declared as `declared`."""
    return CordisError(f'property "{name}" is already declared as {declared}')


def _site(ctx: "Context") -> "Context":
    """Return the context whose restriction chain decides a read of `ctx`."""
    return ctx if ctx.shadow is None else ctx.shadow


def _check_read(site: "Context", name: str) -> None:
    """Raise if a restriction on `site` rejects a read of `name`."""
    rule = site._rejecting(name)
    if rule is not None:
        raise CordisError(f'cannot get property "{name}": {rule}')


def _check_provide(ctx: "Context", name: str) -> None:
    """Raise if a restriction on `ctx` rejects registering `name`."""
    rule = ctx._rejecting(name, provide=True)
    if rule is not None:
        raise CordisError(f'cannot provide "{name}": {rule}')


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

    @owner_thread("scheduler")
    def has_property(self, name: str) -> bool:
        """Return whether `name` is declared as a service or as an accessor."""
        return name in self._services or name in self._accessors

    @owner_thread("scheduler")
    def read(
        self, ctx: "Context", key: Keyable[Any], receiver: object | None = None
    ) -> Any:
        """Return `ctx[key]`: an accessor's value, or the value of a provider.

        A read of a service from a component's context must be admitted (see
        `def_site` and `admit`); a root context reads without this check. A
        restriction on the def site (see `def_site`) is checked before either: a
        plain accessor is checked by its own name, and a mixin accessor by the
        service name it forwards (see `Accessor.source`). The `internal/get`
        listeners run before the admission, so they also see a read that no fiber
        admits. A service that has a `Tracker` is returned as a view for `ctx`.

        Args:
            ctx: The context that reads.
            key: The accessor or service key.
            receiver: The view through which an associated access came, for an
                accessor.

        Raises:
            CordisError: If a restriction rejects the read, if no fiber admits the
                read, or if the fiber that admits it is not loaded.
        """
        name = key_name(key)
        site = _site(ctx)
        accessor = self._accessors.get(name)
        if accessor is not None:
            _check_read(site, accessor.source or name)
            return accessor.get(ctx, receiver)
        _check_read(site, name)
        if site._fiber.runtime is None:
            return self.get(ctx, name, False)
        return ctx.waterfall(
            "internal/get", ctx, name, lambda: self._checked(ctx, site, name)
        )

    @owner_thread("scheduler")
    def def_site(self, ctx: "Context") -> "Context | None":
        """Return the context whose fiber decides which services `ctx` can read.

        That context is the shadow of `ctx` (see `Context.shadow`) when `ctx` has
        one, so a service method reads what its own fiber declares, not what its
        caller declares. Otherwise it is `ctx`.

        Returns:
            The def site, or `None` when its fiber is the fiber of a root context
            (`root`, `root.isolate(...)`): a read from it is not checked.
        """
        site = _site(ctx)
        return None if site._fiber.runtime is None else site

    @owner_thread("scheduler")
    def admit(self, def_site: "Context", name: str) -> Located:
        """Return the fiber that admits a read of the service `name` from `def_site`.

        A fiber admits the read when it, or an ancestor of it, declares `name` in
        `inject` or provides `name` in the realm of `def_site`. A context that isolates
        `name` again stops the search (see `locate`).

        Args:
            def_site: A context that `def_site` returns.
            name: The service name.

        Returns:
            The admitting fiber and the provider that it committed.

        Raises:
            CordisError: If no fiber admits the read. A note on the error names the
                two ways to declare the read.
        """
        found = locate(def_site, name)
        if found is None:
            error = CordisError(f'cannot get property "{name}" without inject')
            error.add_note(
                f'declare "{name}" in inject, or use ctx.inject(["{name}"], callback) '
                "for an optional service"
            )
            raise error
        return found

    def _checked(self, ctx: "Context", def_site: "Context", name: str) -> Any:
        """Admit `ctx[key]` and return its value; raise if the fiber is not loaded."""
        found = self.admit(def_site, name)
        if found.impl is None:
            raise CordisError(
                f'cannot get required service "{name}" in inactive context'
            )
        return get_traceable(ctx, found.impl.value)

    @owner_thread("scheduler")
    def write(
        self,
        ctx: "Context",
        key: Keyable[Any],
        value: Any,
        receiver: object | None = None,
    ) -> None:
        """Perform `ctx[key] = value`: call an accessor's setter, or set a service."""
        name = key_name(key)
        accessor = self._accessors.get(name)
        if accessor is None:
            if name not in self._services:
                raise CordisError(f'cannot set property "{name}" without provide')
            ctx.waterfall(
                "internal/set", ctx, name, value, lambda: self.set(ctx, name, value)
            )
        elif accessor.set is None:
            raise CordisError(f'property "{name}" is read-only')
        else:
            _check_read(_site(ctx), accessor.source or name)
            accessor.set(ctx, value, receiver)

    @owner_thread("scheduler")
    def checked_get(
        self, ctx: "Context", key: Keyable[Any], strict: bool = True
    ) -> Any:
        """Return `ctx.get(key, strict)`: the value of an admitted read, or `None`.

        A read from a component's context must be admitted (see `def_site` and
        `admit`), and a restriction on the def site is checked first, as for `ctx[key]`
        (see `read`). It gives the provider that the admitting fiber committed, not the
        provider that the store holds now, so it agrees with `ctx[key]`. A root context
        reads without the admission check, as `get` does. A service that has a
        `Tracker` is returned as a view for the use site of `ctx`.

        Returns:
            The value, or `None` when the fiber that declares `key` is not loaded, when
            `strict` hides the provider, or, at a root context, when no provider is
            visible.

        Raises:
            CordisError: If a restriction rejects the read, or no fiber admits it.
        """
        name = key_name(key)
        site = _site(ctx)
        _check_read(site, name)
        if site._fiber.runtime is None:
            return self.get(ctx, key, strict)
        impl = _visible(self.admit(site, name).impl, strict)
        return None if impl is None else get_traceable(ctx.use_site, impl.value)

    @owner_thread("scheduler")
    def get(self, ctx: "Context", key: Keyable[Any], strict: bool = True) -> Any:
        """Return the value of the service `key` that `ctx` sees, or `None`.

        This lookup is not checked: it reads the provider that the realm of `ctx` holds
        now, whether or not a fiber declares the read. A host uses it to see which
        provider a component's context resolves; a component reads with
        `Context.get` or `ctx[key]`. A service that has a `Tracker` is returned as a
        view for the use site of `ctx`.
        """
        impl = self.find(ctx, key_name(key), strict)
        return None if impl is None else get_traceable(ctx.use_site, impl.value)

    @owner_thread("scheduler")
    def set(self, ctx: "Context", key: Keyable[Any], value: Any) -> None:
        """Replace the value of the service `key` that `ctx` sees.

        Raises:
            CordisError: If no provider is visible, or the provider belongs to another
                fiber.
        """
        name = key_name(key)
        impl = self.find(ctx, name, False)
        if impl is None:
            raise CordisError(f'cannot set property "{name}" without provide')
        if impl.ctx._fiber is not ctx._fiber:
            raise CordisError(f'cannot set property "{name}" in multiple fibers')
        impl.value = value

    @owner_thread("scheduler")
    def provide(
        self,
        ctx: "Context",
        key: Keyable[Any],
        value: Any = None,
        check: Callable[[Any], bool] | None = None,
    ) -> EffectHandle:
        """Register `value` as the service `key` on the fiber of `ctx`.

        A fiber that injects `key` uses this provider only while `check(view)` returns
        a true value, where `view` is the value as that fiber's context sees it. A
        `check` that raises is logged, and the fiber does not use the provider.

        Returns:
            The handle of the effect. Its removal waits until every dependent of the
            service has left.

        Raises:
            CordisError: If a restriction on `ctx` rejects `key`, the realm already
                has a provider, or `key` is an accessor.
        """
        name = key_name(key)
        fiber = ctx._fiber

        def setup() -> Callable[[], Deferred[None]]:
            if name in self._accessors:
                raise _already_declared(name, "accessor")
            _check_provide(ctx, name)
            self._services.add(name)
            ctx.declare_realm(name)
            realm = ctx.isolates[name]
            taken = self._store.get(realm)
            if taken is not None:
                raise CordisError(
                    f'service "{name}" has been registered at <{taken.ctx._fiber.name}>'
                )
            self._impl_counter += 1
            impl = Impl(self._impl_counter, name, ctx, value, realm, check=check)
            self._store[realm] = impl
            fiber.provisions.add(impl)
            if fiber.state == FiberState.ACTIVE:
                self.notify(ctx, [name])
            return lambda: self._remove(impl)

        return fiber.effect(setup, f"ctx.provide({json.dumps(name)})")

    @owner_thread("scheduler")
    def notify(self, ctx: "Context", names: Iterable[str]) -> None:
        """Report a change of the providers of `names` to `internal/service` listeners.

        Each fiber that injects a changed name in the same realm as `ctx` looks for its
        provider again, and loads or unloads if the provider changed. A listener hears
        the event only when its context uses the same realm of the name as `ctx`.
        """
        names = list(names)
        for runtime in list(ctx._registry.values()):
            for fiber in list(runtime.fibers):
                changed = False
                for name in names:
                    if name not in fiber.inject:
                        continue
                    if fiber.ctx.isolates.get(name) is not ctx.isolates.get(name):
                        continue
                    changed = True
                    fiber._check_impl(name)
                if changed:
                    fiber._refresh()
        for name in names:
            impl = self.find(ctx, name, False)
            ctx._events.emit(
                "internal/service",
                name,
                None if impl is None else impl.value,
                this=_Filter(name, ctx.isolates.get(name)),
            )

    @owner_thread("scheduler")
    def announce(self, fiber: Fiber) -> None:
        """Report that the providers of `fiber` became visible or hidden.

        A fiber calls it when it enters or leaves `ACTIVE`. It notifies for each
        service that the fiber provides and that is still published.
        """
        for impl in list(fiber.provisions):
            if self._store.get(impl.isolate) is impl:
                self.notify(fiber.ctx.isolate(impl.name, impl.isolate), [impl.name])

    @owner_thread("scheduler")
    def accessor(
        self, ctx: "Context", key: Keyable[Any], accessor: Accessor
    ) -> EffectHandle:
        """Register `accessor` as the property `key` on the fiber of `ctx`.

        Raises:
            CordisError: If a restriction on `ctx` rejects `key`, or `key` is already
                declared as a service or an accessor.
        """
        name = key_name(key)

        def setup() -> Callable[[], None]:
            if name in self._services:
                raise _already_declared(name, "service")
            if name in self._accessors:
                raise _already_declared(name, "accessor")
            _check_provide(ctx, name)
            self._accessors[name] = accessor

            def remove() -> None:
                del self._accessors[name]

            return remove

        return ctx._fiber.effect(setup, f"ctx.accessor({json.dumps(name)})")

    @owner_thread("scheduler")
    def mixin(
        self,
        ctx: "Context",
        source: Keyable[Any],
        mixins: Iterable[str] | Mapping[str, str],
    ) -> EffectHandle:
        """Register an accessor for each attribute of the service `source`.

        An accessor reads or sets the attribute on the service value that the reading
        context sees. It gives `None` while no provider is visible. A restriction on
        `ctx` checks each attribute name against its provide rule, and each read or
        write of the accessor against `source`'s read rule (see `Accessor.source`).
        """
        source_name = key_name(source)
        entries = (
            mixins.items() if isinstance(mixins, Mapping) else ((m, m) for m in mixins)
        )

        def setup() -> Iterator[EffectHandle]:
            for attribute, name in list(entries):
                yield self.accessor(ctx, name, self._forward(source_name, attribute))

        return ctx._fiber.effect(setup, f"ctx.mixin({json.dumps(source_name)})")

    @owner_thread("scheduler")
    def bind[**P, R](
        self, ctx: "Context", callback: Callable[P, R], *, site: "Context | None" = None
    ) -> Callable[P, R]:
        """Return a function that calls `callback` with arguments prepared for `ctx`.

        An argument that has a `Tracker` reaches `callback` as its view for `ctx`, so an
        effect that `callback` registers through it belongs to the fiber of `ctx`.

        When the def site of `site` is restricted, each argument also goes through
        `expose`, so `callback` gets views and `Hidden` placeholders in place of fibers,
        foreign contexts, and provider records. `site` defaults to `ctx`.
        """
        receiver = ctx if site is None else site

        @functools.wraps(callback)
        def bound(*args: P.args, **kwargs: P.kwargs) -> R:
            traced = [expose(receiver, get_traceable(ctx, arg)) for arg in args]
            return callback(*traced, **kwargs)  # type: ignore[arg-type]

        return bound

    @owner_thread("scheduler")
    def find(self, ctx: "Context", name: str, strict: bool) -> Impl | None:
        """Return the provider of `name` that `ctx` sees, or `None`.

        With `strict`, a provider whose fiber is not `ACTIVE` gives `None`.
        """
        realm = ctx.isolates.get(name)
        return _visible(None if realm is None else self._store.get(realm), strict)

    def _remove(self, impl: Impl) -> Deferred[None]:
        """Unpublish `impl`, then wait until its dependents leave."""
        ctx = impl.ctx
        if self._store.get(impl.isolate) is impl:
            del self._store[impl.isolate]
        self.notify(ctx.isolate(impl.name, impl.isolate), [impl.name])
        with self.scheduler.call():
            return start(self.scheduler, self._release(impl), fiber=ctx._fiber)

    async def _release(self, impl: Impl) -> None:
        # Every removal waits at least one step, so that the unload of a child can still
        # read the service before the provision is dropped.
        gate = Deferred[None]()
        impl.release = lambda: gate.resolve(None)
        if not impl.dependents:
            gate.resolve(None)
        await gate
        impl.release = None
        impl.ctx._fiber.provisions.discard(impl)

    def _forward(self, source: str, attribute: str) -> Accessor:
        def get(ctx: "Context", receiver: object | None) -> Any:
            service = self.read(ctx, source)
            if service is None:
                return None
            if receiver is None:
                return getattr(service, attribute)
            return get_with_receiver(service, attribute, Overlay(receiver, service))

        def set_(ctx: "Context", value: Any, receiver: object | None) -> None:
            service = self.read(ctx, source)
            setattr(
                service if receiver is None else Overlay(receiver, service),
                attribute,
                value,
            )

        return Accessor(get, set_, source=source)
