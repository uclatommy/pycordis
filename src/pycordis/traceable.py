"""Traceable views: one service object, seen from the context that uses it.

A service holds the context of the component that defines it (the *def site*). Code
that reads the service through another context (the *use site*) gets a view of the
service instead of the service itself. The def site decides which providers the
service code resolves. The use site decides isolation, intercept configuration, and
the fiber that owns the effects that the service code registers.

A service opts in with a `Tracker` in its `cordis_tracker` attribute. `Service` does it
for its subclasses. Start with `Tracker` and `get_traceable`.

A view forwards attribute access to the service, with these differences:

- The tracked property (normally `ctx`) reads as the use-site context.
- A method runs with a `self` whose tracked property is a context that resolves
  services at the def site and acts for the use site. The result of the call is made
  traceable for the use site.
- `cordis_caller` reads as the def-site context, and `cordis_original` as the service.
- `isinstance(view, ServiceClass)` is true.

When the use site is restricted (see `Context.shadow` and `restrict`), every value that
leaves a view goes through `expose`, the arguments of a call go through `receive`, and a
service class can declare an `Exposure` to hide or to pass members. When the service
is restricted and the reader is not, the arguments of a call go through `expose` and
the results through `receive`; when both are restricted, the two rules apply in turn.
The methods of the service read the unfiltered values. A method of a restricted service
gets a `ContextView` as its context.

`RealmView` and `realm_filter` give a `cordis_filter` hook of restricted code the realm
labels of listener contexts and nothing else.

`expose` and `receive` are the two crossings between a restricted component and the
rest of the application: `expose` gives the component a value that it can use, and
`receive` gives the application the real object back for a value that the component
returns or passes on.
"""

import functools
import inspect
from collections.abc import AsyncGenerator, Callable, Coroutine, Generator, Iterator
from dataclasses import dataclass
from types import (
    AsyncGeneratorType,
    BuiltinFunctionType,
    CoroutineType,
    FunctionType,
    GeneratorType,
    MappingProxyType,
    MethodType,
)
from typing import TYPE_CHECKING, Any

from pycordis.deferred import Deferred
from pycordis.errors import CordisError

if TYPE_CHECKING:
    from pycordis.context import Context

type _Cross = Callable[["Context", Any], Any]

_MISSING: Any = object()
_slot = object.__getattribute__
_VIEW_NAMES = ("cordis_original", "cordis_caller", "__class__")
# The names that the exit of a view never crosses. `cordis_caller` crosses: a reader
# from outside gets its own context through `expose`.
_PASS_NAMES = ("cordis_original", "__class__")


@dataclass(frozen=True, slots=True)
class Tracker:
    """How an object takes part in traceable views.

    Set an instance of this class as the `cordis_tracker` attribute of an object that
    holds a context.

    Attributes:
        property: The name of the attribute that holds the context. A view gives the
            use-site context under this name. `None` if the object has no context.
        associate: A service name. A view reads the attribute `name` from the property
            `"<associate>.<name>"` when a service or an accessor of that name exists.
        no_shadow: When true, a method runs with the view itself as `self`, and its
            result is not made traceable.

    Examples:
        ```python
        >>> class Session:
        ...     cordis_tracker = Tracker(property="ctx", associate="session")
        ...
        ...     def __init__(self, ctx):
        ...         self.ctx = ctx
        ```
    """

    property: str | None = None
    associate: str | None = None
    no_shadow: bool = False


class Hidden:
    """The value that a restricted component gets in place of an object it cannot use.

    A read or write of an attribute, a call, iteration, and indexing raise
    `CordisError` that names the type of the hidden object. A listener that receives a
    `Hidden` is still called, so a restricted component can ignore the argument without
    a change to event dispatch.

    A protocol name (`__class__`, `__reduce_ex__`, and the other `__name__` forms)
    reads as on any object, so `isinstance`, `copy`, and `pickle` work, and a missing
    protocol name raises `AttributeError`. `copy.copy` and `copy.deepcopy` give the
    same `Hidden`. A pickled `Hidden` keeps only the type name: `receive` of an
    unpickled one raises `CordisError`.

    When a component gives a `Hidden` back to the application, `receive` returns the
    hidden object.
    """

    __slots__ = ("_kind", "_target")

    def __init__(self, kind: str, target: object = _MISSING) -> None:
        """Create a placeholder for `target`, an object of the type named `kind`."""
        object.__setattr__(self, "_kind", kind)
        object.__setattr__(self, "_target", target)

    def _refuse(self, action: str) -> CordisError:
        kind = object.__getattribute__(self, "_kind")
        return CordisError(f"cannot {action} a hidden {kind}")

    def __getattribute__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            return object.__getattribute__(self, name)
        raise Hidden._refuse(self, f'read "{name}" of')

    def __reduce__(self) -> tuple[type["Hidden"], tuple[str]]:
        return (Hidden, (object.__getattribute__(self, "_kind"),))

    def __copy__(self) -> "Hidden":
        return self  # a placeholder is its own copy

    def __deepcopy__(self, memo: dict[int, Any]) -> "Hidden":
        return self

    def __setattr__(self, name: str, value: Any) -> None:
        raise Hidden._refuse(self, f'set "{name}" of')

    def __delattr__(self, name: str) -> None:
        raise Hidden._refuse(self, f'delete "{name}" of')

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise Hidden._refuse(self, "call")

    def __iter__(self) -> Any:
        raise Hidden._refuse(self, "iterate")

    def __getitem__(self, key: Any) -> Any:
        raise Hidden._refuse(self, "index")

    def __repr__(self) -> str:
        return f"Hidden <{object.__getattribute__(self, '_kind')}>"


def _hook(value: object) -> Callable[[object, "Context"], object] | None:
    """Return the `cordis_expose` hook of the class of `value`, or `None`.

    The hook is read from the class, so a view whose `__class__` shows a service class
    is not taken for that service.
    """
    return getattr(type(value), "cordis_expose", None)  # optional on any class


def _cross_deferred(
    scope: "Context", source: Deferred[Any], cross: Callable[["Context", Any], Any]
) -> Deferred[Any]:
    """Return a `Deferred` that settles with `cross` applied to the result of `source`.

    `source` can complete on any thread. On the owner thread the crossing runs when
    `source` completes, or at once when `source` is done. On another thread the
    crossing runs on the owner thread in a later scheduler step, so a crossing never
    runs on a foreign thread.
    """
    settled = Deferred[Any]()

    def complete() -> None:
        try:
            result = source.result()
        except BaseException as error:
            settled.reject(error)
        else:
            settled.resolve(cross(scope, result))

    def on_done(_: Deferred[Any]) -> None:
        scheduler = scope.scheduler
        if scheduler.is_owner():
            complete()
        else:
            scheduler.submit(complete)

    source.add_done_callback(on_done)
    return settled


async def _cross_coroutine(
    scope: "Context", source: Coroutine[Any, Any, Any], outbound: _Cross
) -> Any:
    """Await `source` and give its result through `outbound`."""
    return outbound(scope, await source)


def _cross_generator(
    scope: "Context",
    source: Generator[Any, Any, Any],
    inbound: _Cross,
    outbound: _Cross,
) -> Generator[Any, Any, Any]:
    """Give each item and the return value of `source` through `outbound`.

    A value sent in goes through `inbound`. `throw` and `close` reach `source`.
    """
    try:
        item = source.send(None)
        while True:
            try:
                sent = yield outbound(scope, item)
            except GeneratorExit:
                source.close()
                raise
            except BaseException as error:
                item = source.throw(error)
            else:
                item = source.send(inbound(scope, sent))
    except StopIteration as stop:
        return outbound(scope, stop.value)


async def _cross_async_generator(
    scope: "Context",
    source: AsyncGenerator[Any, Any],
    inbound: _Cross,
    outbound: _Cross,
) -> AsyncGenerator[Any, Any]:
    """Give each item of `source` through `outbound`.

    A value sent in goes through `inbound`. `athrow` and `aclose` reach `source`.
    """
    try:
        item = await source.asend(None)
        while True:
            try:
                sent = yield outbound(scope, item)
            except GeneratorExit:
                await source.aclose()
                raise
            except BaseException as error:
                item = await source.athrow(error)
            else:
                item = await source.asend(inbound(scope, sent))
    except StopAsyncIteration:
        return


def _cross_later(scope: "Context", value: Any, to_host: bool) -> Any:
    """Return the wrapper of a value that gives its results later, or `_MISSING`.

    A `Deferred`, a coroutine, a generator, and an async generator give the values that
    they produce through `receive` when `to_host` is true, and through `expose`
    otherwise.
    """
    outbound, inbound = (_receive, _expose) if to_host else (_expose, _receive)
    kind = type(value)
    if kind is Deferred:
        return _cross_deferred(scope, value, outbound)
    if kind is CoroutineType:
        return _cross_coroutine(scope, value, outbound)
    if kind is GeneratorType:
        return _cross_generator(scope, value, inbound, outbound)
    if kind is AsyncGeneratorType:
        return _cross_async_generator(scope, value, inbound, outbound)
    return _MISSING


_FUNCTION_FORMS = (FunctionType, MethodType, BuiltinFunctionType, functools.partial)
_CONTAINERS = (tuple, list, set, frozenset)
_LATER_FORMS = (Deferred, CoroutineType, GeneratorType, AsyncGeneratorType)


class _Crossing:
    """A function that carries its arguments and its result across the boundary.

    One class serves both directions. A crossing that `expose` made wraps an
    application function for a restricted caller: each argument goes through `receive`
    and the result through `expose`. A crossing that `receive` made wraps a restricted
    function for the application: each argument goes through `expose` and the result
    through `receive`. A crossing equals, and hashes as, the function that it wraps.

    A crossing has no `__weakref__` slot: each crossing makes a new wrapper, so a weak
    reference to one would die at once. `weakref.ref` raises `TypeError` for it.
    """

    __slots__ = ("_function", "_scope", "_to_host", "_name", "_is_coroutine_marker")

    def __init__(
        self, scope: "Context", function: Callable[..., Any], to_host: bool
    ) -> None:
        self._function = function
        self._scope = scope
        self._to_host = to_host
        self._name: str | None = None
        if inspect.iscoroutinefunction(function):
            inspect.markcoroutinefunction(self)

    @property
    def __wrapped__(self) -> Callable[..., Any]:
        return self._function

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        scope = self._scope
        inbound, outbound = (
            (_expose, _receive) if self._to_host else (_receive, _expose)
        )
        result = self._function(
            *[inbound(scope, arg) for arg in args],
            **{key: inbound(scope, arg) for key, arg in kwargs.items()},
        )
        if self._name is not None:
            return _read_exit(scope, self._name, result)
        return outbound(scope, result)

    def __eq__(self, other: object) -> bool:
        if type(other) is _Crossing:
            other = other._function
        return self._function == other

    def __hash__(self) -> int:
        return hash(self._function)

    def __repr__(self) -> str:
        return repr(self._function)


def expose[T](receiver: "Context", value: T) -> T:
    """Return what code in the context `receiver` can use in place of `value`.

    This is the crossing from the application to a restricted component. The scope of
    `receiver` is its def site. When that scope carries no restriction, `value` is
    returned unchanged. Otherwise:

    - An object whose class has a `cordis_expose(value, receiver)` hook gives the
      result of the hook: a `Fiber` gives a `FiberView`, a `Context` that the scope
      owns gives a `ContextView`, and a `Context` that the scope does not own, or a
      provider record, gives a `Hidden`.
    - A service view gives the view of the same service for `receiver`, so an effect
      that the component registers through it belongs to the component's fiber.
    - A method of an object that its hook replaces gives a `Hidden`: a method of a
      `Fiber`, of a provider record, or of a `Context`.
    - A function, a bound or builtin method, or a `functools.partial` gives a function
      that equals it. A call of that function passes each argument through `receive`
      and gives the result through `expose`.
    - A `tuple`, `list`, `dict`, `set`, or `frozenset` gives a new container of the
      same type, with each element, key, and value exposed. A change to the new
      container is not visible through the old one. A read-only mapping (such as
      `ctx.meta`) gives a new read-only mapping in the same way.
    - A `Deferred` gives a new `Deferred` that settles with the exposed result, or with
      the same error. On the owner thread it settles when `value` completes, and at
      once when `value` is done. When `value` completes on another thread, it settles
      on the owner thread in a later scheduler step.
    - A coroutine gives a coroutine whose result is exposed. A generator gives a
      generator, and an async generator gives an async generator: each item (and the
      return value of a generator) is exposed, a value that the caller sends in goes
      through `receive`, and `throw` and `close` reach the source.
    - A view, a `Hidden`, and a function that `expose` made are unchanged. A function
      that `receive` made for the scope of `receiver` gives the function that it wraps;
      one that `receive` made for another scope gives a function that wraps it.
    - Any other object, also a callable object that is not a function, is returned
      unchanged, so its type, its attributes, and its identity stay the same.

    The result has the type of `value` for a caller: a `FiberView` and a `Hidden` are
    not instances of that type. Call this function on the owner thread.

    Args:
        receiver: The context of the component that receives the value.
        value: Any object.
    """
    if not receiver._def_site._restrictions:
        return value
    return _expose(receiver, value)


def _expose(scope: "Context", value: Any) -> Any:
    """Apply the exposure rule of `expose` to `value` for a restricted scope."""
    hook = _hook(value)
    if hook is not None:
        return hook(value, scope)
    kind = type(value)
    if kind is _Crossing:
        if not value._to_host:
            return value
        if value._scope._def_site is scope._def_site:
            return value._function
        return _Crossing(scope, value, to_host=False)  # made for another scope
    if issubclass(kind, TraceableView):
        return get_traceable(scope, value)
    if isinstance(value, MethodType):
        owner = value.__self__
        owner_hook = _hook(owner)
        if inspect.isfunction(owner_hook) and owner_hook(owner, scope) is not owner:
            return Hidden(f"method of {type(owner).__name__}", value)
    if kind in _CONTAINERS:
        return kind(_expose(scope, item) for item in value)
    if kind is dict:
        return {
            _expose(scope, key): _expose(scope, item) for key, item in value.items()
        }
    if kind is MappingProxyType:
        return MappingProxyType(
            {_expose(scope, key): _expose(scope, item) for key, item in value.items()}
        )
    if kind in _LATER_FORMS:
        return _cross_later(scope, value, to_host=False)
    if isinstance(value, _FUNCTION_FORMS):
        return _Crossing(scope, value, to_host=False)
    return value


def receive[T](scope: "Context", value: T) -> T:
    """Return what the application can use in place of `value`, which a component gave.

    This is the crossing from a restricted component to the application, the inverse
    of `expose`. The scope of `scope` is its def site. When that scope carries no
    restriction, `value` is returned unchanged. Otherwise:

    - A view of an object (a `FiberView`, an exposed view, a service view) and a
      `Hidden` give the real object that they show, for any scope.
    - A function that `expose` made gives the function that it wraps.
    - Any other function, bound or builtin method, or `functools.partial` gives a
      function that equals it. A call of that function passes each argument through
      `expose` and gives the result through `receive`.
    - A `tuple`, `list`, `dict`, `set`, `frozenset`, or read-only mapping gives a new
      container of the same type, with each element, key, and value received.
    - A `Deferred`, a coroutine, a generator, and an async generator give the same
      kind of object, with each result, item, and return value received and each value
      that is sent in exposed (the `Deferred` rule of `expose` applies to threads).
    - Any other object is returned unchanged.

    Call this function on the owner thread.

    Args:
        scope: The context of the component that gave the value.
        value: Any object.

    Raises:
        CordisError: If `value` is a `Hidden` that lost its object, such as an
            unpickled one.
    """
    if not scope._def_site._restrictions:
        return value
    return _receive(scope, value)


def _receive(scope: "Context", value: Any) -> Any:
    """Apply the rule of `receive` to `value` for a restricted scope."""
    kind = type(value)
    if kind is Hidden:
        target = object.__getattribute__(value, "_target")
        if target is _MISSING:
            raise CordisError(
                f"cannot give back a hidden {value!r}: it lost its object"
            )
        return target
    if kind is _Crossing:
        return value if value._to_host else value._function
    slot = getattr(kind, "cordis_view", None)  # optional on any class
    if slot is not None:
        return object.__getattribute__(value, slot)
    if issubclass(kind, _Forwarder):
        return get_original(value)
    if kind in _CONTAINERS:
        return kind(_receive(scope, item) for item in value)
    if kind is dict:
        return {
            _receive(scope, key): _receive(scope, item) for key, item in value.items()
        }
    if kind is MappingProxyType:
        return MappingProxyType(
            {_receive(scope, key): _receive(scope, item) for key, item in value.items()}
        )
    if kind in _LATER_FORMS:
        return _cross_later(scope, value, to_host=True)
    if isinstance(value, _FUNCTION_FORMS):
        return _Crossing(scope, value, to_host=True)
    return value


@dataclass(frozen=True, slots=True)
class Exposure:
    """How a class gives itself to a restricted component.

    Set an instance as the `cordis_expose` attribute of a class. `expose` calls the
    instance with the object and the receiving context, and the result is what the
    restricted component gets in place of the object.

    A service class that has a `Tracker` keeps its view: the view applies the exposure
    at its exits. Any other object reaches a restricted component as a read-only view
    that forwards reads to the object. A method of the object runs on the object
    itself, so the object reads its own data without the filter. Only what the
    restricted component reads from the view is filtered.

    The view also forwards `len`, iteration, `[]`, `in`, and `bool` to the object, and
    filters the items that iteration and `[]` give. Two views of the same object for
    the same scope are equal.

    A class that is callable gives a view that is callable: a call passes each argument
    through `receive` and gives the result through the same filter, and `"__call__"` in
    `hidden` or `safe` applies to the call. With `"__call__"` in `safe`, the call passes
    its arguments and its result unchanged, as a `safe` method does. A bound method of
    the object that the component gets from a listener argument receives its arguments
    through `receive` and gives its result through `expose`.

    Each value that leaves the view goes through `expose`, so a `Fiber` is a
    `FiberView`, a foreign `Context` or a provider record is a `Hidden`, a container is
    filtered element by element, and the result of a method is filtered in the same
    way. A value that `expose` does not recognize is not changed.

    Attributes:
        hidden: Names that raise `CordisError` when a restricted component reads them.
        safe: Names whose values pass unchanged, with no filter.

    Examples:
        ```python
        >>> class Account:
        ...     cordis_expose = Exposure(hidden=frozenset({"token"}))
        ...
        ...     def __init__(self, ctx):
        ...         self.ctx = ctx
        ...         self.token = "secret"
        ```
    """

    hidden: frozenset[str] = frozenset()
    safe: frozenset[str] = frozenset()

    def __call__(self, value: object, receiver: "Context") -> object:
        """Return the exposed view of `value` for the scope of `receiver`."""
        traced = get_traceable(receiver, value)
        if traced is not value:
            return traced
        cls = _CallableExposedView if callable(value) else _ExposedView
        return cls(receiver, value)


def _exposure_of(target: object) -> Exposure | None:
    """Return the `Exposure` that the class of `target` declares, or `None`."""
    attr = inspect.getattr_static(type(target), "cordis_expose", None)
    return attr if isinstance(attr, Exposure) else None


def _hidden_error(name: str, hidden: Hidden) -> CordisError:
    kind = object.__getattribute__(hidden, "_kind")
    return CordisError(f'"{name}" gives a hidden {kind} in a restricted context')


def _read_exit(scope: "Context", name: str, value: Any) -> Any:
    """Return `expose(scope, value)`, or raise if the result is a `Hidden`.

    This is the one place where a `Hidden` becomes an error: a read of a view or of a
    context gives a value to use at once, so a value that the component cannot use
    fails at the read. A function that `expose` wraps raises in the same way when its
    result is a `Hidden`.
    """
    result = expose(scope, value)
    if type(result) is Hidden:
        raise _hidden_error(name, result)
    if type(result) is _Crossing and result is not value:
        result._name = name
    return result


def _exit(ctx: "Context", target: object, name: str, read: Callable[[], Any]) -> Any:
    """Return `read()` filtered for the restricted scope of `ctx`.

    The names of the view protocol (`cordis_original`, `__class__`) are not filtered.
    `cordis_caller` is: a reader from outside gets a `ContextView` of its own context,
    and a `CordisError` for any other context.
    """
    if name in _PASS_NAMES:
        return read()
    exposure = _exposure_of(target)
    if exposure is not None:
        if name in exposure.hidden:
            raise CordisError(f'cannot read "{name}" in a restricted context')
        if name in exposure.safe:
            return read()
    return _read_exit(ctx, name, read())


def get_traceable[T](ctx: "Context", value: T) -> T:
    """Return the view of `value` for the context `ctx`, or `value` itself.

    `value` is returned unchanged when it has no `Tracker`. A view or a service that
    already has a view is traced again for `ctx`, so a view never wraps a view.

    Args:
        ctx: The use-site context. It can be a context that resolves services at
            another site (see `Context.shadow`).
        value: Any object.
    """
    tracker = getattr(value, "cordis_tracker", None)  # optional on any object
    if not isinstance(tracker, Tracker):
        return value
    return create_traceable(ctx, get_original(value), tracker)  # type: ignore[return-value]


def create_traceable(ctx: "Context", value: Any, tracker: Tracker) -> Any:
    """Return a view of `value` for `ctx`. A callable value gives a callable view."""
    cls = CallableTraceableView if callable(value) else TraceableView
    return cls(ctx, value, tracker)


def get_original(value: Any) -> Any:
    """Return the object that a view or a shadow shows, or `value` if it is neither."""
    if isinstance(value, _Forwarder):
        return value.cordis_original
    return value


def _is_hook(name: str) -> bool:
    """Return whether `name` is a hook or a special name: it is never traced."""
    return name.startswith(("cordis_", "__"))


def _own(target: object) -> dict[str, Any]:
    try:
        return vars(target)
    except TypeError:  # an object with `__slots__` has no instance table
        return {}


def _reflect_get(target: object, name: str, receiver: object) -> Any:
    """Read `name` of `target` with `receiver` as `self` for methods and properties."""
    attr = inspect.getattr_static(type(target), name, _MISSING)
    if isinstance(attr, property):
        if attr.fget is None:
            raise AttributeError(f"property {name!r} has no getter")
        return attr.fget(receiver)
    own = _own(target)
    if name in own:
        return own[name]
    if isinstance(attr, FunctionType):
        return MethodType(attr, receiver)
    return getattr(target, name)


def _stored_context(target: object, name: str) -> "Context | None":
    """Return the context that `target` holds in the attribute `name`, if it is data."""
    attr = inspect.getattr_static(type(target), name, _MISSING)
    if isinstance(attr, property):
        return None
    own = _own(target)
    if name in own:
        return _real(own[name])
    return None if attr is _MISSING else _real(attr)


def _real(value: Any) -> Any:
    """Return the context that a `ContextView` shows; any other value is unchanged."""
    from pycordis.context import real_context

    return real_context(value)


def _service_site(target: object, prop: str | None) -> "Context | None":
    """Return the restricted def site of the service `target`, or `None`.

    The def site of a service is the context that it holds in `prop`. It is fixed when
    the service is built, so a view reads it once.
    """
    stored = None if prop is None else _stored_context(target, prop)
    if stored is None:
        return None
    site = stored._def_site
    return site if site._restrictions else None


def _view_of_context(ctx: "Context") -> Any:
    """Return the `ContextView` of `ctx`: the `self.ctx` of restricted service code."""
    from pycordis.context import ContextView

    return ContextView(ctx)


def _inward(service: "Context | None", value: Any) -> Any:
    """Return `receive(service, value)`, or `value` when the service is unrestricted."""
    return value if service is None else receive(service, value)


def _create_shadow(
    use_site: "Context", target: object, prop: str | None, receiver: Any
) -> Any:
    """Return an object that reads as `receiver`, with `prop` as a shadow context.

    The shadow context acts for `use_site` and resolves services at the def site that
    `target` holds in `prop`.
    """
    if prop is None:
        return receiver
    stored = _stored_context(target, prop)
    if stored is None:
        return receiver
    def_site = stored if stored.shadow is None else stored.shadow
    ctx = use_site.with_shadow(def_site)
    if def_site._restrictions:
        ctx = _view_of_context(ctx)  # the method body is restricted code
    return _shadow(receiver, prop, ctx)


def _shadow(receiver: Any, prop: str | None, ctx: "Context") -> "_Shadow":
    """Return the shadow of the view `receiver` that reads `prop` as `ctx`."""
    cls = _CallableShadow if callable(receiver) else _Shadow
    return cls(receiver, prop, ctx)


def _interior(
    view: "TraceableView", ctx: "Context", tracker: Tracker, service: "Context | None"
) -> Any:
    """Return the `self` of a `no_shadow` method of `view`.

    The method of a restricted service is restricted code: its context is a
    `ContextView` of the use site with the service's def site. For an unrestricted
    service, a restricted reader gets a shadow that reads without the filter, so the
    method sees real objects, as a shadowed method does. Otherwise `self` is the view.
    """
    if service is not None:
        site = _view_of_context(ctx.use_site.with_shadow(service))
        return _shadow(view, tracker.property, site)
    if not ctx._def_site._restrictions:
        return view
    return _shadow(view, tracker.property, ctx.use_site)


def _trace_result(use_site: "Context", function: Callable[..., Any]) -> Any:
    """Wrap `function` so that its result is traceable for `use_site`."""

    @functools.wraps(function)
    def call(*args: Any, **kwargs: Any) -> Any:
        return get_traceable(use_site, function(*args, **kwargs))

    return call


def _finish(use_site: "Context", tracker: Tracker, value: Any) -> Any:
    """Make an attribute value traceable: a tracked object, or a callable."""
    inner = getattr(value, "cordis_tracker", None)  # optional on any object
    if isinstance(inner, Tracker):
        return create_traceable(use_site, get_original(value), inner)
    if not tracker.no_shadow and callable(value) and not isinstance(value, type):
        return _trace_result(use_site, value)
    return value


def _read_attribute(
    view: "TraceableView",
    ctx: "Context",
    target: object,
    tracker: Tracker,
    name: str,
    service: "Context | None",
) -> Any:
    """Read `name`; a restricted `service` gives its values through `receive` first.

    `receive` comes before the tracing, so a service that a method returns is still a
    view for the use site.
    """
    use_site = ctx.use_site
    if tracker.associate is not None:
        associated = f"{tracker.associate}.{name}"
        if use_site._reflect.has_property(associated):
            # `ctx`, not `use_site`: one def site governs `view.bar`, `ctx["foo.bar"]`
            return ctx._reflect.read(ctx, associated, view)
    attr = inspect.getattr_static(type(target), name, _MISSING)
    if isinstance(attr, property):
        shadow = _create_shadow(use_site, target, tracker.property, view)
        value = _reflect_get(target, name, shadow)
        return _finish(use_site, tracker, _inward(service, value))
    if name not in _own(target) and isinstance(attr, FunctionType):
        if tracker.no_shadow:
            method = MethodType(attr, _interior(view, ctx, tracker, service))
            return _inward(service, method)
        shadow = _create_shadow(use_site, target, tracker.property, view)
        return _trace_result(use_site, _inward(service, MethodType(attr, shadow)))
    return _finish(use_site, tracker, _inward(service, getattr(target, name)))


def get_with_receiver(value: Any, name: str, receiver: object) -> Any:
    """Read the attribute `name` of `value`, with `receiver` as `self`.

    A method gets `receiver` as its first argument, and a property getter runs with
    `receiver`. `value` can be a view: the read uses the original object.
    """
    return _reflect_get(get_original(value), name, receiver)


def _realm_accept(accept: Callable[[Any], Any]) -> Callable[["Context"], Any]:
    """Return a `cordis_filter` function that gives `accept` a `RealmView`."""

    def cordis_filter(ctx: "Context") -> Any:
        return accept(RealmView(ctx))

    return cordis_filter


def _read_view(
    view: "TraceableView", name: str, service: "Context | None" = None
) -> Any:
    """Read `name` of `view`.

    The path of a service's own methods gives no `service`, and reads with no crossing.
    The exit of a view of a restricted `service` gives its def site, and the value goes
    through `receive`.
    """
    ctx = _slot(view, "_ctx")
    target = _slot(view, "_target")
    tracker = _slot(view, "_tracker")
    if name == "cordis_original":
        return target
    if name == "cordis_caller":
        return ctx if ctx.shadow is None else ctx.shadow
    if name == tracker.property:
        return ctx.use_site
    if name == "__class__":
        return type(target)
    if _is_hook(name):
        value = _reflect_get(target, name, _hook_self(view, ctx, target, tracker))
        if service is None:
            return value
        if name == "cordis_filter":
            return _realm_accept(value)
        return receive(service, value)
    return _read_attribute(view, ctx, target, tracker, name, service)


def _method_self(
    view: "TraceableView", ctx: "Context", target: object, tracker: Tracker
) -> Any:
    """Return the `self` of a method that runs for `view` and reads its own context."""
    if tracker.no_shadow:
        return _interior(view, ctx, tracker, _slot(view, "_service"))
    return _create_shadow(ctx.use_site, target, tracker.property, view)


def _hook_self(
    view: "TraceableView", ctx: "Context", target: object, tracker: Tracker
) -> Any:
    """Return the `self` of a hook that the machinery calls on `view`.

    When a side is restricted, a hook reads its own context as a method does, so
    `self.cordis_caller` is its real def site and not a read through the exit. With no
    restriction the view is `self`.
    """
    if _slot(view, "_service") is None and not ctx._def_site._restrictions:
        return view
    return _method_self(view, ctx, target, tracker)


def _invoke_view(view: "CallableTraceableView", args: Any, kwargs: Any) -> Any:
    """Call `view`, with no filter on the result."""
    ctx = _slot(view, "_ctx")
    target = _slot(view, "_target")
    tracker = _slot(view, "_tracker")
    if inspect.getattr_static(target, "cordis_invoke", _MISSING) is _MISSING:
        return target(*args, **kwargs)
    receiver = _method_self(view, ctx, target, tracker)
    return _reflect_get(target, "cordis_invoke", receiver)(*args, **kwargs)


class _Forwarder:
    """The base of the objects that forward their attributes to a service."""

    __slots__ = ()


class TraceableView(_Forwarder):
    """The view of an object for one use-site context.

    Create a view with `get_traceable`. A view is not a copy: it reads and writes the
    attributes of the original object, which `cordis_original` gives.
    `isinstance(view, cls)` follows the original object, but `type(view)` is the view
    class, so check the type of a service with `isinstance`.

    Attributes:
        cordis_original: The object that the view shows.
        cordis_caller: The def-site context: the context through which code read the
            view. Resolution of the object's own dependencies uses it. A restricted
            reader gets a `ContextView` of its own context, and a `CordisError` for any
            other context.

    The direction of the crossing depends on which side is restricted. A restricted
    reader of an unrestricted service passes its arguments through `receive` and gets
    results through `expose`. An unrestricted reader of a restricted service passes its
    arguments through `expose` and gets results through `receive`. When both are
    restricted, an argument goes through `receive` of the reader and `expose` of the
    service, and a result goes the other way. A method of a restricted service runs
    with a `ContextView` as its context.
    """

    __slots__ = ("_ctx", "_target", "_tracker", "_service")

    def __init__(self, ctx: "Context", target: object, tracker: Tracker) -> None:
        _set = object.__setattr__
        _set(self, "_ctx", ctx)
        _set(self, "_target", target)
        _set(self, "_tracker", tracker)
        _set(self, "_service", _service_site(target, tracker.property))

    def __getattribute__(self, name: str) -> Any:
        ctx = _slot(self, "_ctx")
        service = _slot(self, "_service")
        if name in _PASS_NAMES:
            return _read_view(self, name)
        if not ctx._def_site._restrictions:
            return _read_view(self, name, service)
        target = _slot(self, "_target")
        return _exit(ctx, target, name, lambda: _read_view(self, name, service))

    def __setattr__(self, name: str, value: Any) -> None:
        ctx = _slot(self, "_ctx")
        target = _slot(self, "_target")
        tracker = _slot(self, "_tracker")
        if name in ("cordis_original", "cordis_caller") or name == tracker.property:
            raise AttributeError(f"cannot set {name!r} on a traceable view")
        if _is_hook(name):
            setattr(target, name, value)
            return
        use_site = ctx.use_site
        if tracker.associate is not None:
            associated = f"{tracker.associate}.{name}"
            if use_site._reflect.has_property(associated):
                ctx._reflect.write(ctx, associated, value, self)
                return
        attr = inspect.getattr_static(type(target), name, _MISSING)
        if isinstance(attr, property):
            if attr.fset is None:
                raise AttributeError(f"property {name!r} has no setter")
            shadow = _create_shadow(use_site, target, tracker.property, self)
            attr.fset(shadow, value)
        else:
            setattr(target, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(_slot(self, "_target"), name)

    def __repr__(self) -> str:
        return repr(_slot(self, "_target"))


class CallableTraceableView(TraceableView):
    """The view of a callable object.

    A call runs the `cordis_invoke` hook of the object with a `self` as a method gets,
    or calls the object when it has no hook.
    """

    __slots__ = ()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        ctx = _slot(self, "_ctx")
        service = _slot(self, "_service")
        reader = ctx._def_site._restrictions
        if service is None and not reader:
            return _invoke_view(self, args, kwargs)
        if reader:
            args = tuple(receive(ctx, arg) for arg in args)
            kwargs = {key: receive(ctx, arg) for key, arg in kwargs.items()}
        if service is not None:
            args = tuple(expose(service, arg) for arg in args)
            kwargs = {key: expose(service, arg) for key, arg in kwargs.items()}

        def call() -> Any:
            return _inward(service, _invoke_view(self, args, kwargs))

        if not reader:
            return call()
        return _exit(ctx, _slot(self, "_target"), "__call__", call)


class _Shadow(_Forwarder):
    """A view whose tracked property reads as a shadow context.

    A method of a service runs with a shadow as `self`, so that `self.ctx` is a context
    that resolves services at the def site and acts for the use site.
    """

    __slots__ = ("_view", "_property", "_ctx")

    def __init__(self, view: Any, prop: str | None, ctx: "Context") -> None:
        _set = object.__setattr__
        _set(self, "_view", view)
        _set(self, "_property", prop)
        _set(self, "_ctx", ctx)

    def __getattribute__(self, name: str) -> Any:
        if name == _slot(self, "_property"):
            return _slot(self, "_ctx")
        view = _slot(self, "_view")
        if _is_hook(name) and name not in _VIEW_NAMES:
            # a hook runs with this shadow as `self`
            return _reflect_get(view.cordis_original, name, self)
        return _read_view(view, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == _slot(self, "_property"):
            raise AttributeError(f"cannot set {name!r} on a traceable view")
        setattr(_slot(self, "_view"), name, value)

    def __delattr__(self, name: str) -> None:
        delattr(_slot(self, "_view"), name)

    def __repr__(self) -> str:
        return repr(_slot(self, "_view"))


class _CallableShadow(_Shadow):
    __slots__ = ()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _invoke_view(_slot(self, "_view"), args, kwargs)


class _ExposedView(_Forwarder):
    """A read-only view of an object that declares an `Exposure`.

    A method runs on the original object. Each value that leaves the view is filtered
    for the restricted scope of the receiving context.
    """

    __slots__ = ("_ctx", "_target")

    def __init__(self, ctx: "Context", target: object) -> None:
        _set = object.__setattr__
        _set(self, "_ctx", ctx)
        _set(self, "_target", target)

    def __getattribute__(self, name: str) -> Any:
        target = _slot(self, "_target")
        if name == "cordis_original":
            return target
        if name == "__class__":
            return type(target)
        return _exit(_slot(self, "_ctx"), target, name, lambda: getattr(target, name))

    def __setattr__(self, name: str, value: Any) -> None:
        raise CordisError(f'cannot set "{name}" in a restricted context')

    def __delattr__(self, name: str) -> None:
        raise CordisError(f'cannot delete "{name}" in a restricted context')

    # Protocol operations are read from the class, so `__getattribute__` does not see
    # them. Each one reads the object and filters what it gives.

    def __len__(self) -> int:
        return len(_slot(self, "_target"))

    def __bool__(self) -> bool:
        return bool(_slot(self, "_target"))

    def __contains__(self, item: object) -> bool:
        ctx = _slot(self, "_ctx")
        return receive(ctx, item) in _slot(self, "_target")

    def __getitem__(self, key: object) -> Any:
        ctx = _slot(self, "_ctx")
        return _read_exit(ctx, "__getitem__", _slot(self, "_target")[receive(ctx, key)])

    def __iter__(self) -> Iterator[Any]:
        ctx = _slot(self, "_ctx")
        return (_read_exit(ctx, "__iter__", item) for item in _slot(self, "_target"))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ExposedView):
            return NotImplemented
        return (
            _slot(self, "_target") is _slot(other, "_target")
            and _slot(self, "_ctx")._def_site is _slot(other, "_ctx")._def_site
        )

    def __hash__(self) -> int:
        return hash((id(_slot(self, "_target")), id(_slot(self, "_ctx")._def_site)))

    def __repr__(self) -> str:
        return repr(_slot(self, "_target"))


class _CallableExposedView(_ExposedView):
    """The view of a callable object that declares an `Exposure`.

    A call passes each argument through `receive`, runs the object, and gives the
    result through the exit rule, so `"__call__"` in `Exposure.hidden` or
    `Exposure.safe` applies to the call as it does to any other name. A `safe` call
    passes its arguments unchanged, as `view.__call__(...)` does with the unfiltered
    bound method.
    """

    __slots__ = ()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        ctx = _slot(self, "_ctx")
        target = _slot(self, "_target")
        exposure = _exposure_of(target)
        if exposure is None or "__call__" not in exposure.safe:
            args = tuple(receive(ctx, arg) for arg in args)
            kwargs = {key: receive(ctx, arg) for key, arg in kwargs.items()}
        return _exit(ctx, target, "__call__", lambda: target(*args, **kwargs))


class RealmView:
    """The context of a listener, as a restricted `cordis_filter` sees it.

    A `cordis_filter` hook selects listeners by the realm of their context. A hook that
    restricted code defines reads the contexts of listeners that the component does not
    own, so it gets this view in place of a `Hidden`. The view gives `isolates` and no
    other member: every other name raises `CordisError`.

    Examples:
        ```python
        >>> RealmView(ctx).isolates == ctx.isolates
        True
        >>> RealmView(ctx).provide
        Traceback (most recent call last):
        pycordis.errors.CordisError: cannot read "provide" of a realm view
        ```
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: "Context") -> None:
        """Create the view of the listener context `ctx`."""
        object.__setattr__(self, "_ctx", ctx)

    def __getattribute__(self, name: str) -> Any:
        if name == "isolates":
            return _slot(self, "_ctx").isolates
        if name.startswith("__") and name.endswith("__"):
            return object.__getattribute__(self, name)  # a protocol name
        raise CordisError(f'cannot read "{name}" of a realm view')

    def __setattr__(self, name: str, value: Any) -> None:
        raise CordisError(f'cannot set "{name}" of a realm view')

    def __delattr__(self, name: str) -> None:
        raise CordisError(f'cannot delete "{name}" of a realm view')

    def __repr__(self) -> str:
        return f"RealmView of {_slot(self, '_ctx')!r}"


class _RealmFilter:
    """An object whose `cordis_filter` gives a `RealmView` to the filter it wraps."""

    __slots__ = ("cordis_filter",)

    def __init__(self, accept: Callable[[Any], Any]) -> None:
        self.cordis_filter = _realm_accept(accept)


def realm_filter(scope: "Context", this: object) -> object:
    """Return the `this` argument of a dispatch for the listeners of a restricted scope.

    A dispatch calls `this.cordis_filter(ctx)` with the context of each listener. When
    the def site of `scope` is restricted and `this` has that hook, the result is an
    object whose hook calls the hook of `this` with a `RealmView` of the context, so
    the filter reads the realm labels and nothing else. Otherwise `this` is returned
    unchanged. Call this function on the owner thread.

    Args:
        scope: The context of the component that dispatches, or that defines `this`.
        this: The `this` argument of the dispatch.
    """
    if not scope._def_site._restrictions:
        return this
    accept = getattr(this, "cordis_filter", None)  # optional on any object
    if accept is None:
        return this
    return _RealmFilter(accept)


class Overlay:
    """The `self` of a method that a service adds to another object.

    Reads give the attributes of `service` first, and then those of `receiver`. A write
    to a property of the service class runs its setter with this object as `self`;
    every other write goes to `receiver`, so the state that a forwarded method stores
    belongs to the object that it extends. A mixin gives this object to the methods
    that it forwards.
    """

    __slots__ = ("_receiver", "_service")

    def __init__(self, receiver: object, service: object) -> None:
        object.__setattr__(self, "_receiver", receiver)
        object.__setattr__(self, "_service", service)

    def __getattribute__(self, name: str) -> Any:
        try:
            return getattr(_slot(self, "_service"), name)
        except AttributeError:
            return getattr(_slot(self, "_receiver"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        service = get_original(_slot(self, "_service"))
        attr = inspect.getattr_static(type(service), name, _MISSING)
        if not isinstance(attr, property):
            setattr(_slot(self, "_receiver"), name, value)
        elif attr.fset is None:
            raise AttributeError(f"property {name!r} has no setter")
        else:
            attr.fset(self, value)
