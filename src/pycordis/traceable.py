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
leaves a view goes through `expose`, and a service class can declare an `Exposure` to
hide or to pass members. The methods of the service read the unfiltered values.
`screen` applies the same filter to a value that `ctx[key]` or `ctx.get(key)` reads.

`expose` and `receive` are the two crossings between a restricted component and the
rest of the application: `expose` gives the component a value that it can use, and
`receive` gives the application the real object back for a value that the component
returns or passes on.
"""

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import BuiltinFunctionType, FunctionType, MethodType
from typing import TYPE_CHECKING, Any

from pycordis.deferred import Deferred
from pycordis.errors import CordisError

if TYPE_CHECKING:
    from pycordis.context import Context

_MISSING: Any = object()
_slot = object.__getattribute__
_VIEW_NAMES = ("cordis_original", "cordis_caller", "__class__")


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

    `source` can complete on any thread. The crossing runs on the owner thread, in a
    scheduler step after `source` completes, also when `source` completes on the owner
    thread: one path for every thread keeps the crossing off the completing thread.
    """
    settled = Deferred[Any]()

    def complete() -> None:
        try:
            result = source.result()
        except BaseException as error:
            settled.reject(error)
        else:
            settled.resolve(cross(scope, result))

    source.add_done_callback(lambda _: scope.scheduler.submit(complete))
    return settled


_FUNCTION_FORMS = (FunctionType, MethodType, BuiltinFunctionType, functools.partial)
_CONTAINERS = (tuple, list, set, frozenset)


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
      result of the hook: a `Fiber` gives a `FiberView`, and a `Context` that the
      scope does not own, or a provider record, gives a `Hidden`.
    - A service view gives the view of the same service for `receiver`, so an effect
      that the component registers through it belongs to the component's fiber.
    - A method of an object that its hook replaces gives a `Hidden`: a method of a
      `Fiber` or of a provider record always, and a method of a `Context` that the
      scope does not own.
    - A function, a bound or builtin method, or a `functools.partial` gives a function
      that equals it. A call of that function passes each argument through `receive`
      and gives the result through `expose`.
    - A `tuple`, `list`, `dict`, `set`, or `frozenset` gives a new container of the
      same type, with each element, key, and value exposed. A change to the new
      container is not visible through the old one.
    - A `Deferred` gives a new `Deferred` that settles with the exposed result, or with
      the same error. It settles one scheduler step after `value`, so code that awaits
      it resumes after code that awaits `value` directly.
    - A view, a `Hidden`, and a function that `expose` made are unchanged. A function
      that `receive` made gives the function that it wraps.
    - Any other object is returned unchanged.

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
        return value._function if value._to_host else value
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
    if kind is Deferred:
        return _cross_deferred(scope, value, _expose)
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
    - A `tuple`, `list`, `dict`, `set`, or `frozenset` gives a new container of the
      same type, with each element, key, and value received.
    - A `Deferred` gives a new `Deferred` that settles with the received result, or with
      the same error.
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
    if kind is Deferred:
        return _cross_deferred(scope, value, _receive)
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
        return _ExposedView(receiver, value)


def _exposure_of(target: object) -> Exposure | None:
    """Return the `Exposure` that the class of `target` declares, or `None`."""
    attr = inspect.getattr_static(type(target), "cordis_expose", None)
    return attr if isinstance(attr, Exposure) else None


def _hidden_error(name: str, hidden: Hidden) -> CordisError:
    kind = object.__getattribute__(hidden, "_kind")
    return CordisError(f'"{name}" gives a hidden {kind} in a restricted context')


def screen[T](receiver: "Context", name: str, value: T) -> T:
    """Return what a read of `name` gives to code in the context `receiver`.

    `Context.__getitem__` and `Context.get` pass the value that they read through this
    function, so a provided value or an accessor value is filtered as a value that
    leaves a service view is. When the def site of `receiver` carries no restriction,
    `value` is returned unchanged. Otherwise `value` goes through `expose`, and a
    `Hidden` result raises `CordisError`. When `value` is a function, the result of a
    call of the function that this function gives raises in the same way. Call this
    function on the owner thread.

    Args:
        receiver: The context that reads.
        name: The service or accessor name, for the error message.
        value: The value that the read gives.

    Raises:
        CordisError: If the value is a context that the scope does not own, a provider
            record, or a method that `expose` hides.
    """
    return _read_exit(receiver, name, value)


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

    The names of the view protocol (`cordis_original`, `cordis_caller`, `__class__`)
    are not filtered.
    """
    if name in _VIEW_NAMES:
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
        return own[name]
    return None if attr is _MISSING else attr


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
    return _shadow(receiver, prop, use_site.with_shadow(def_site))


def _shadow(receiver: Any, prop: str | None, ctx: "Context") -> "_Shadow":
    """Return the shadow of the view `receiver` that reads `prop` as `ctx`."""
    cls = _CallableShadow if callable(receiver) else _Shadow
    return cls(receiver, prop, ctx)


def _interior(view: "TraceableView", ctx: "Context", tracker: Tracker) -> Any:
    """Return the `self` of a `no_shadow` method of `view`.

    A restricted scope gets a shadow that reads without the filter, so the method sees
    real objects, as a shadowed method does. Otherwise `self` is the view.
    """
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
    view: "TraceableView", ctx: "Context", target: object, tracker: Tracker, name: str
) -> Any:
    use_site = ctx.use_site
    if tracker.associate is not None:
        associated = f"{tracker.associate}.{name}"
        if use_site._reflect.has_property(associated):
            # `ctx`, not `use_site`: one def site governs `view.bar`, `ctx["foo.bar"]`
            return ctx._reflect.read(ctx, associated, view)
    attr = inspect.getattr_static(type(target), name, _MISSING)
    if isinstance(attr, property):
        shadow = _create_shadow(use_site, target, tracker.property, view)
        return _finish(use_site, tracker, _reflect_get(target, name, shadow))
    if name not in _own(target) and isinstance(attr, FunctionType):
        if tracker.no_shadow:
            return MethodType(attr, _interior(view, ctx, tracker))
        shadow = _create_shadow(use_site, target, tracker.property, view)
        return _trace_result(use_site, MethodType(attr, shadow))
    return _finish(use_site, tracker, getattr(target, name))


def get_with_receiver(value: Any, name: str, receiver: object) -> Any:
    """Read the attribute `name` of `value`, with `receiver` as `self`.

    A method gets `receiver` as its first argument, and a property getter runs with
    `receiver`. `value` can be a view: the read uses the original object.
    """
    return _reflect_get(get_original(value), name, receiver)


def _read_view(view: "TraceableView", name: str) -> Any:
    """Read `name` of `view`, with no filter: the path of a service's own methods."""
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
        return _reflect_get(target, name, view)
    return _read_attribute(view, ctx, target, tracker, name)


def _invoke_view(view: "CallableTraceableView", args: Any, kwargs: Any) -> Any:
    """Call `view`, with no filter on the result."""
    ctx = _slot(view, "_ctx")
    target = _slot(view, "_target")
    tracker = _slot(view, "_tracker")
    if inspect.getattr_static(target, "cordis_invoke", _MISSING) is _MISSING:
        return target(*args, **kwargs)
    receiver = (
        _interior(view, ctx, tracker)
        if tracker.no_shadow
        else _create_shadow(ctx.use_site, target, tracker.property, view)
    )
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
            view. Resolution of the object's own dependencies uses it.
    """

    __slots__ = ("_ctx", "_target", "_tracker")

    def __init__(self, ctx: "Context", target: object, tracker: Tracker) -> None:
        _set = object.__setattr__
        _set(self, "_ctx", ctx)
        _set(self, "_target", target)
        _set(self, "_tracker", tracker)

    def __getattribute__(self, name: str) -> Any:
        ctx = _slot(self, "_ctx")
        if not ctx._def_site._restrictions:
            return _read_view(self, name)
        return _exit(ctx, _slot(self, "_target"), name, lambda: _read_view(self, name))

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
        if not ctx._def_site._restrictions:
            return _invoke_view(self, args, kwargs)
        return _exit(
            ctx,
            _slot(self, "_target"),
            "__call__",
            lambda: _invoke_view(self, args, kwargs),
        )


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

    def __repr__(self) -> str:
        return repr(_slot(self, "_target"))


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
