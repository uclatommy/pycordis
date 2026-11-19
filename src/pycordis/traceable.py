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
"""

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import FunctionType, MethodType
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

    Every attribute access, call, and iteration raises `CordisError` that names the
    type of the hidden object. A listener that receives a `Hidden` is still called, so
    a restricted component can ignore the argument without a change to event dispatch.
    """

    __slots__ = ("_kind",)

    def __init__(self, kind: str) -> None:
        """Create a placeholder for an object of the type named `kind`."""
        object.__setattr__(self, "_kind", kind)

    def _refuse(self, action: str) -> CordisError:
        kind = object.__getattribute__(self, "_kind")
        return CordisError(f"cannot {action} a hidden {kind}")

    def __getattribute__(self, name: str) -> Any:
        if name == "__class__":  # `isinstance` reads it
            return Hidden
        raise Hidden._refuse(self, f'read "{name}" of')

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


def _expose_deferred(receiver: "Context", source: Deferred[Any]) -> Deferred[Any]:
    """Return a `Deferred` that settles with the exposed result of `source`.

    `source` can complete on any thread. The exposure runs on the owner thread.
    """
    settled = Deferred[Any]()

    def complete() -> None:
        try:
            result = source.result()
        except BaseException as error:
            settled.reject(error)
        else:
            settled.resolve(expose(receiver, result))

    source.add_done_callback(lambda _: receiver.scheduler.submit(complete))
    return settled


def expose[T](receiver: "Context", value: T) -> T:
    """Return what code in the context `receiver` can use in place of `value`.

    The scope of `receiver` is its def site. When that scope carries no restriction,
    `value` is returned unchanged. Otherwise:

    - An object whose class has a `cordis_expose(value, receiver)` hook gives the
      result of the hook: a `Fiber` gives a `FiberView`, and a `Context` that the
      scope does not own, or a provider record, gives a `Hidden`.
    - A method of such an object gives a `Hidden`.
    - A `tuple`, `list`, `dict`, `set`, or `frozenset` gives a new container of the
      same type, with each element, key, and value exposed.
    - A `Deferred` gives a new `Deferred` that settles with the exposed result, or with
      the same error.
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


def _expose(receiver: "Context", value: Any) -> Any:
    """Apply the exposure rule of `expose` to `value` for a restricted scope."""
    hook = _hook(value)
    if hook is not None:
        return hook(value, receiver)
    if isinstance(value, MethodType) and inspect.isfunction(_hook(value.__self__)):
        return Hidden(f"method of {type(value.__self__).__name__}")
    kind = type(value)
    if kind in (tuple, list, set, frozenset):
        return kind(_expose(receiver, item) for item in value)
    if kind is dict:
        return {
            _expose(receiver, key): _expose(receiver, item)
            for key, item in value.items()
        }
    if kind is Deferred:
        return _expose_deferred(receiver, value)
    return value


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
    cls = _CallableShadow if callable(receiver) else _Shadow
    return cls(receiver, prop, use_site.with_shadow(def_site))


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
            return MethodType(attr, view)
        shadow = _create_shadow(use_site, target, tracker.property, view)
        return _trace_result(use_site, MethodType(attr, shadow))
    return _finish(use_site, tracker, getattr(target, name))


def get_with_receiver(value: Any, name: str, receiver: object) -> Any:
    """Read the attribute `name` of `value`, with `receiver` as `self`.

    A method gets `receiver` as its first argument, and a property getter runs with
    `receiver`. `value` can be a view: the read uses the original object.
    """
    return _reflect_get(get_original(value), name, receiver)


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
        target = _slot(self, "_target")
        tracker = _slot(self, "_tracker")
        if name == "cordis_original":
            return target
        if name == "cordis_caller":
            return ctx if ctx.shadow is None else ctx.shadow
        if name == tracker.property:
            return ctx.use_site
        if name == "__class__":
            return type(target)
        if _is_hook(name):
            return _reflect_get(target, name, self)
        return _read_attribute(self, ctx, target, tracker, name)

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
        target = _slot(self, "_target")
        tracker = _slot(self, "_tracker")
        if inspect.getattr_static(target, "cordis_invoke", _MISSING) is _MISSING:
            return target(*args, **kwargs)  # type: ignore[operator]
        receiver = (
            self
            if tracker.no_shadow
            else _create_shadow(ctx.use_site, target, tracker.property, self)
        )
        return _reflect_get(target, "cordis_invoke", receiver)(*args, **kwargs)


class _Shadow(_Forwarder):
    """A view whose tracked property reads as a shadow context.

    A method of a service runs with a shadow as `self`, so that `self.ctx` is a context
    that resolves services at the def site and acts for the use site.
    """

    __slots__ = ("_view", "_property", "_ctx")

    def __init__(self, view: Any, prop: str, ctx: "Context") -> None:
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
        return getattr(view, name)

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
        return _slot(self, "_view")(*args, **kwargs)


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
