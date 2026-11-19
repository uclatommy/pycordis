"""`restrict`: which inject, read, and provide keys a derived context admits."""

import copy
import functools
import inspect
import logging
import pickle
import weakref
from collections.abc import AsyncGenerator, Callable, Generator, Iterator
from pathlib import Path
from types import MappingProxyType

import pytest
from loader_fakes import RecordingLoader

from pycordis.context import Context, ContextView, Rule, real_context, restrict
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import Fiber, FiberState, FiberView
from pycordis.hmr import Hmr, HmrConfig, ReloadResult
from pycordis.loader import Loader
from pycordis.reflect import Impl
from pycordis.registry import inject as inject_method
from pycordis.scheduler import ImmediateScheduler, start
from pycordis.service import Service
from pycordis.traceable import (
    Exposure,
    Hidden,
    RealmView,
    Tracker,
    expose,
    get_traceable,
    realm_filter,
    receive,
)


def _no_deps(ctx: Context, config: object) -> None:
    return None


def _needs_db(ctx: Context, config: object) -> None:
    return None


_needs_db.inject = ["db"]


class TestArguments:
    """`restrict`'s validation of its own arguments."""

    def test_every_dimension_none_raises_value_error(self, root: Context) -> None:
        with pytest.raises(ValueError, match="at least one of"):
            restrict(root)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"inject": "db"},
            {"inject_prefixes": "plugin."},
            {"provide": "db"},
            {"provide_prefixes": "plugin."},
        ],
        ids=["inject", "inject_prefixes", "provide", "provide_prefixes"],
    )
    def test_a_bare_str_in_place_of_an_iterable_raises_type_error(
        self, root: Context, kwargs: dict[str, object]
    ) -> None:
        with pytest.raises(TypeError, match="not a bare str"):
            restrict(root, **kwargs)

    def test_a_provide_dimension_with_no_rule_admits_every_provided_key(
        self, root: Context
    ) -> None:
        restricted = restrict(root, inject=["other"])
        # the provide dimension has no rule: any name can be provided
        restricted.provide("anything", 1)

    def test_an_inject_dimension_with_no_rule_admits_every_read(
        self, root: Context
    ) -> None:
        restricted = restrict(root, provide=["allowed"])
        restricted.provide("allowed", 1)
        # the inject dimension has no rule: any name can be read
        assert restricted.get("allowed") == 1

    @pytest.mark.parametrize(
        "rule, name, admitted",
        [
            (Rule(frozenset(), ()), "anything", False),
            (Rule(frozenset({"db"}), ()), "db", True),
            (Rule(frozenset({"db"}), ()), "other", False),
            (Rule(frozenset(), ("plugin.",)), "plugin.cache", True),
            (Rule(frozenset(), ("plugin.",)), "other", False),
        ],
        ids=["empty", "name match", "name miss", "prefix match", "prefix miss"],
    )
    def test_rule_admits_by_exact_name_or_prefix(
        self, rule: Rule, name: str, admitted: bool
    ) -> None:
        assert rule.admits(name) is admitted


def _via_component(root: Context, ctx: Context) -> Fiber:
    return ctx.use(_needs_db)


def _via_inject_method(root: Context, ctx: Context) -> Fiber:
    class Owner:
        def __init__(self, ctx: Context, config: object) -> None:
            pass

        @inject_method("db")
        def warm(self) -> None:
            return None

    owner = ctx.use(Owner)
    return owner.children[0]


def _via_ctx_inject(root: Context, ctx: Context) -> Fiber:
    return ctx.inject(["db"], lambda inner: None)


def _via_replace(root: Context, ctx: Context) -> Fiber:
    fiber = ctx.use(_no_deps)
    root.registry.replace(_no_deps, _needs_db)
    return fiber


@pytest.mark.parametrize(
    "via",
    [_via_component, _via_inject_method, _via_ctx_inject, _via_replace],
    ids=[
        "component inject",
        "an @inject method",
        "ctx.inject(deps, callback)",
        "a replacement",
    ],
)
def test_inject_outside_the_rule_fails_the_fiber(
    root: Context, via: Callable[[Context, Context], Fiber]
) -> None:
    restricted = restrict(root, inject=["other"])
    sibling = restricted.use(lambda ctx, config: None)

    fiber = via(root, restricted)

    assert fiber.state == FiberState.FAILED
    message = str(fiber.error)
    assert '"db"' in message
    assert "other" in message
    assert sibling.state == FiberState.ACTIVE
    assert root.fiber.state == FiberState.ACTIVE


def test_inject_prefixes_admits_a_matching_name(root: Context) -> None:
    restricted = restrict(root, inject_prefixes=["plugin."])

    def needs_plugin_cache(ctx: Context, config: object) -> None:
        return None

    needs_plugin_cache.inject = ["plugin.cache"]

    admitted = restricted.use(needs_plugin_cache)
    rejected = restricted.use(_needs_db)

    # the admitted fiber passes the inject check and parks at PENDING, since
    # nothing provides "plugin.cache"; the point is that it is not FAILED
    assert admitted.state == FiberState.PENDING
    assert rejected.state == FiberState.FAILED


def test_a_child_fibers_context_carries_the_restriction(root: Context) -> None:
    restricted = restrict(root, inject=["other"])
    captured: dict[str, Fiber] = {}

    def outer(ctx: Context, config: object) -> None:
        captured["child"] = ctx.use(_needs_db)

    restricted.use(outer)

    assert captured["child"].state == FiberState.FAILED


@pytest.mark.parametrize(
    "derive", [lambda ctx: ctx.isolate("x"), lambda ctx: ctx.extend(host=1)]
)
def test_an_isolated_or_extended_context_carries_the_restriction(
    root: Context, derive: Callable[[Context], Context]
) -> None:
    restricted = restrict(root, inject=["other"])
    fiber = derive(restricted).use(_needs_db)
    assert fiber.state == FiberState.FAILED


def test_a_nested_restrict_admits_only_the_intersection(root: Context) -> None:
    restricted = restrict(root, inject=["other"])

    # a nested restrict that tries to widen the rule still cannot inject "db"
    widened = restrict(restricted, inject=["other", "db"])
    assert widened.use(_needs_db).state == FiberState.FAILED

    # a nested restrict can only narrow: "other" is no longer admitted either
    def _needs_other(ctx: Context, config: object) -> None:
        return None

    _needs_other.inject = ["other"]
    narrowed = restrict(restricted, inject=[])
    assert narrowed.use(_needs_other).state == FiberState.FAILED


class TestReads:
    """A read, a write, or an accessor outside the rule raises `CordisError`."""

    @pytest.mark.parametrize(
        "access", [lambda ctx: ctx["db"], lambda ctx: ctx.get("db")], ids=["[]", "get"]
    )
    def test_a_read_outside_the_rule_raises(
        self, root: Context, access: Callable[[Context], object]
    ) -> None:
        root.provide("db", 1)
        restricted = restrict(root, inject=["other"])

        with pytest.raises(CordisError, match='"db"') as excinfo:
            access(restricted)
        assert "other" in str(excinfo.value)

    def test_a_restricted_components_own_context_checks_its_reads(
        self, root: Context
    ) -> None:
        restricted = restrict(root, inject=["other"])
        captured: dict[str, Context] = {}

        def component(ctx: Context, config: object) -> None:
            captured["ctx"] = ctx

        fiber = restricted.use(component)
        assert fiber.state == FiberState.ACTIVE

        with pytest.raises(CordisError, match='"db"'):
            captured["ctx"]["db"]

    def test_a_shadowed_read_is_checked_at_the_def_site_not_the_use_site(
        self, root: Context
    ) -> None:
        root.provide("db", 1)
        unrestricted_service = root.inject(["db"], lambda ctx: None).ctx
        restricted_caller = restrict(root, inject=["other"])

        # the def site (the service's own context) admits "db": the caller's
        # restriction, which rejects it, does not apply
        shadowed = restricted_caller.with_shadow(unrestricted_service)
        assert shadowed["db"] == 1

        restricted_service = (
            restrict(root, inject=["other"]).inject(["other"], lambda ctx: None).ctx
        )

        # the def site rejects "db" even though the caller (root) has no restriction
        with pytest.raises(CordisError, match='"db"'):
            root.with_shadow(restricted_service)["db"]

    @pytest.mark.parametrize(
        "declare, key, checked_name",
        [
            (
                lambda root: root.accessor(
                    "gauge", lambda ctx, r: 1, lambda ctx, v, r: None
                ),
                "gauge",
                "gauge",
            ),
            (
                lambda root: (
                    root.provide("timer", object()),
                    root.mixin("timer", ["sleep"]),
                ),
                "sleep",
                "timer",
            ),
        ],
        ids=["plain accessor", "mixin accessor"],
    )
    def test_an_accessor_outside_the_rule_raises_by_the_checked_name(
        self,
        root: Context,
        declare: Callable[[Context], object],
        key: str,
        checked_name: str,
    ) -> None:
        declare(root)
        restricted = restrict(root, inject=["other"])

        for access in (
            lambda: restricted[key],
            lambda: restricted.__setitem__(key, 2),
        ):
            with pytest.raises(CordisError, match=f'"{checked_name}"') as excinfo:
                access()
            if checked_name != key:
                assert key not in str(excinfo.value)


@pytest.mark.parametrize(
    "register",
    [
        lambda ctx: ctx.provide("blocked", 1),
        lambda ctx: ctx.accessor("blocked", lambda ctx, receiver: 1),
        lambda ctx: ctx.mixin("db", ["blocked"]),
    ],
    ids=["provide", "accessor", "mixin"],
)
def test_provide_accessor_or_mixin_outside_the_rule_fails_the_fiber(
    root: Context, register: Callable[[Context], object]
) -> None:
    restricted = restrict(root, provide=["allowed"])

    fiber = restricted.use(lambda ctx, config: register(ctx))

    assert fiber.state == FiberState.FAILED
    assert '"blocked"' in str(fiber.error)
    assert "allowed" in str(fiber.error)
    assert fiber.get_effects() == []


@pytest.mark.parametrize(
    "read",
    [
        lambda ctx: ctx.root,
        lambda ctx: ctx.reflect,
        lambda ctx: ctx.registry,
        lambda ctx: ctx.events,
    ],
    ids=["root", "reflect", "registry", "events"],
)
@pytest.mark.divergence("context-restriction")
def test_a_restricted_context_gives_the_host_the_real_services_of_the_application(
    root: Context, read: Callable[[Context], object]
) -> None:
    restricted = restrict(root, inject=["other"])

    assert read(restricted) is read(root)


@pytest.mark.parametrize("register", ["on", "once"])
def test_a_listener_of_a_restricted_context_gets_real_objects(
    root: Context, register: str
) -> None:
    received: list[object] = []
    getattr(restrict(root, inject=["other"]), register)("greet", received.append)

    root.emit("greet", root.fiber)

    assert received == [root.fiber] and type(received[0]) is Fiber


def test_on_and_emit_are_not_restricted(root: Context) -> None:
    restricted = restrict(root, inject=["other"], provide=["other"])
    received: list[str] = []

    restricted.on("greet", lambda who: received.append(who))
    restricted.emit("greet", "world")

    assert received == ["world"]


@pytest.mark.divergence("context-restriction")
def test_a_restricted_fiber_is_a_view_that_gives_only_the_allow_list(
    root: Context,
) -> None:
    """The view has no `ctx` or `parent`, so `fiber.parent.fiber` and the host's
    unrestricted contexts are out of reach."""
    restricted = restrict(root, inject=["other"])
    host = root.use(_no_deps)
    view = ContextView(restricted).fiber

    assert isinstance(view, FiberView) and not isinstance(view, Fiber)
    assert (view.name, view.uid, view.state) == ("root", 0, FiberState.ACTIVE)
    assert view.error is None
    assert all(isinstance(child, FiberView) for child in view.children)
    assert host.uid in [child.uid for child in view.children]
    assert view.get_effects() == root.fiber.get_effects()
    for name in ("ctx", "parent", "committed", "runtime", "logger"):
        with pytest.raises(CordisError, match=f'"{name}"'):
            getattr(view, name)
    # a protocol probe is a missing attribute, not a rule violation
    assert not hasattr(view, "__fspath__")
    assert copy.copy(view) is view and copy.deepcopy(view) is view
    for name in ("_owned", "_target", "name"):
        with pytest.raises(CordisError, match=f'cannot set "{name}"'):
            setattr(view, name, True)
        with pytest.raises(CordisError, match=f'cannot delete "{name}"'):
            delattr(view, name)


@pytest.mark.parametrize("fails", [False, True], ids=["resolves", "rejects"])
def test_wait_and_await_on_a_view_settle_with_the_view(
    root: Context, fails: bool
) -> None:
    view = ContextView(restrict(root, inject=["other"])).use(
        _needs_db if fails else _no_deps
    )

    async def awaited() -> object:
        return await view

    for run in (
        lambda: view.wait().result(),
        lambda: start(root.scheduler, awaited()).result(),
    ):
        if fails:
            with pytest.raises(CordisError) as excinfo:
                run()
            assert excinfo.value is view.error
        else:
            assert run() == view


@pytest.mark.parametrize(
    "act",
    [
        lambda view: view.dispose(),
        lambda view: view.restart(),
        lambda view: view.update(None),
    ],
    ids=["dispose", "restart", "update"],
)
def test_a_view_acts_only_on_a_fiber_that_the_component_owns(
    root: Context, act: Callable[[FiberView], object]
) -> None:
    restricted = restrict(root, inject=["other"])
    sibling = restricted.use(_no_deps)
    host = root.use(_no_deps)
    seen: dict[str, FiberView] = {}

    def plugin(ctx: ContextView, config: object) -> None:
        seen["own"] = ctx.use(_no_deps)
        seen["grandchild"] = restrict(ctx, inject=["other"]).use(_no_deps)
        # views of fibers outside the subtree, for the scope of this plugin
        scope = real_context(ctx)
        seen["sibling"] = expose(scope, sibling)
        seen["host"] = expose(scope, host)
        seen["root"] = expose(scope, root.fiber)

    assert restricted.use(plugin).state == FiberState.ACTIVE

    for name in ("own", "grandchild"):
        act(seen[name])
    for name in ("sibling", "host", "root"):
        with pytest.raises(CordisError, match="does not own"):
            act(seen[name])


@pytest.mark.divergence("context-restriction")
@pytest.mark.parametrize(
    "read",
    [
        lambda ctx: ctx.use(_no_deps),
        lambda ctx: ctx.inject([], lambda inner: None),
        lambda ctx: ctx.fiber,
        lambda ctx: ctx["svc"],
        lambda ctx: ctx.get("svc"),
        lambda ctx: ctx.bail("probe"),
    ],
    ids=["use", "inject", "fiber", "item", "get", "bail"],
)
def test_a_restricted_context_gives_the_host_real_objects_and_a_view_gives_views(
    root: Context, read: Callable[[Context | ContextView], object]
) -> None:
    host_fiber = root.use(_no_deps)
    root.provide("svc", host_fiber)
    root.on("probe", lambda: host_fiber)
    restricted = restrict(root, inject=["svc"])

    assert isinstance(read(restricted), Fiber)
    assert type(read(ContextView(restricted))) is FiberView


def test_a_view_equals_a_view_of_the_same_fiber_for_the_same_scope(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    other_scope = restrict(root, inject=["other"])
    fiber = restricted.use(_no_deps)
    view = expose(restricted, fiber)

    assert expose(restricted, fiber) == view
    assert hash(expose(restricted, fiber)) == hash(view)
    assert view in {expose(restricted, fiber)}
    assert view != fiber and fiber != view
    assert expose(other_scope, fiber) != view


@pytest.mark.divergence("context-restriction")
def test_a_restricted_plugin_reading_loader_ctx_gets_the_use_site(
    root: Context,
) -> None:
    """Closes the `Loader` escape: `ctx['loader'].ctx` gives the use site, not the
    loader's own unrestricted context."""
    root.use(Loader)
    restricted = restrict(root, inject=["loader"])
    captured: dict[str, ContextView] = {}

    def component(ctx: ContextView, config: object) -> None:
        captured["own"] = ctx
        captured["loader_ctx"] = ctx["loader"].ctx

    component.inject = ["loader"]
    fiber = restricted.use(component)

    assert fiber.state == FiberState.ACTIVE
    assert captured["loader_ctx"] == captured["own"]


def test_a_restricted_fibers_provide_notifies_an_existing_dependent(
    root: Context,
) -> None:
    restricted = restrict(root, provide=["svc"])
    dependent = root.inject(["svc"], lambda inner: None)
    assert dependent.state == FiberState.PENDING

    def component(ctx: Context, config: object) -> None:
        ctx.provide("svc", 1)

    fiber = restricted.use(component)

    assert fiber.state == FiberState.ACTIVE
    # the dependent fiber reaches ACTIVE only if the restricted provider's fiber
    # announces its new provision through `ReflectService.notify`
    assert dependent.state == FiberState.ACTIVE


def _calls_of(calls: list[tuple[object, ...]]) -> list[object]:
    return [call[0] for call in calls]


@pytest.mark.divergence("context-restriction")
@pytest.mark.parametrize(
    ("event", "trigger", "pick", "kind"),
    [
        ("internal/plugin", lambda root: root.use(_no_deps), _calls_of, FiberView),
        ("internal/status", lambda root: root.use(_no_deps), _calls_of, FiberView),
        ("internal/config", lambda root: root.use(_no_deps), _calls_of, FiberView),
        (
            "internal/commit",
            lambda root: root.use(_no_deps).update({"a": 1}),
            _calls_of,
            FiberView,
        ),
        (
            "internal/listener",
            lambda root: root.on("greet", lambda: None),
            _calls_of,
            Hidden,
        ),
        (
            "internal/dispatch",
            lambda root: root.emit("greet", root.fiber, (root.fiber,)),
            lambda calls: [
                element
                for call in calls
                if call[1] == "greet"
                for element in (call[2][0], call[2][1][0])
            ],
            FiberView,
        ),
    ],
    ids=["plugin", "status", "config", "commit", "listener", "dispatch"],
)
def test_a_restricted_listener_gets_views_and_placeholders_in_place_of_host_objects(
    root: Context,
    event: str,
    trigger: Callable[[Context], object],
    pick: Callable[[list[tuple[object, ...]]], list[object]],
    kind: type,
) -> None:
    calls: list[tuple[object, ...]] = []
    ContextView(restrict(root, inject=["other"])).on(
        event, lambda *args: calls.append(args), True
    )

    trigger(root)

    objects = pick(calls)
    assert objects and all(type(obj) is kind for obj in objects)
    for obj in objects:
        with pytest.raises(CordisError):
            _ = obj.ctx  # a view and a placeholder both refuse
    host_views = [obj for obj in objects if isinstance(obj, FiberView)]
    for view in host_views:
        with pytest.raises(CordisError, match="does not own"):
            view.dispose()
    # no host fiber went down through the listener
    assert root.fiber.state == FiberState.ACTIVE
    assert all(child.state == FiberState.ACTIVE for child in root.fiber.children)


def test_a_restricted_listener_gets_the_contexts_that_its_component_owns(
    root: Context,
) -> None:
    seen: list[object] = []
    own: list[Context] = []

    def plugin(ctx: Context, config: object) -> None:
        ctx.on("internal/listener", lambda target, *rest: seen.append(target))
        ctx.on("greet", lambda: None)
        child = restrict(ctx, inject=["other"])
        child.use(lambda inner, cfg: inner.on("greet", lambda: None))
        own.append(real_context(ctx))

    restrict(root, inject=["other"]).use(plugin)

    assert type(seen[0]) is ContextView and real_context(seen[0]) is own[0]
    assert len(seen) == 2 and type(seen[1]) is ContextView


class _Plain:
    pass


@pytest.mark.parametrize(
    "build",
    [
        lambda fiber: (fiber, fiber),
        lambda fiber: [fiber, [fiber]],
        lambda fiber: {fiber: fiber},
        lambda fiber: {fiber},
        lambda fiber: frozenset({fiber}),
        lambda fiber: MappingProxyType({fiber: [fiber]}),
    ],
    ids=["tuple", "list", "dict", "set", "frozenset", "mappingproxy"],
)
def test_expose_filters_the_elements_of_a_builtin_container(
    root: Context, build: Callable[[Fiber], object]
) -> None:
    restricted = restrict(root, inject=["other"])
    value = build(root.fiber)

    result = expose(restricted, value)

    assert type(result) is type(value) and result is not value
    assert _leaves(result) and all(type(leaf) is FiberView for leaf in _leaves(result))
    assert expose(root, value) is value
    # a scope whose def site is unrestricted gets the real objects
    assert expose(restricted.with_shadow(root), value) is value


def _leaves(value: object) -> list[object]:
    """Return the non-container elements, including dict keys, of a nested value."""
    if isinstance(value, (dict, MappingProxyType)):
        return [leaf for item in (*value, *value.values()) for leaf in _leaves(item)]
    if isinstance(value, (tuple, list, set, frozenset)):
        return [leaf for item in value for leaf in _leaves(item)]
    return [value]


@pytest.mark.parametrize("member", ["meta", "intercepts"])
def test_a_host_object_in_meta_or_intercepts_does_not_reach_a_restricted_plugin(
    root: Context, member: str
) -> None:
    host = root.use(lambda c, cfg: None)
    app = root.extend(app=root, f=host)
    app = app.intercept("x", {"app": root, "f": host})
    seen: list[object] = []

    def plugin(ctx: Context, config: object) -> None:
        mapping = getattr(ctx, member)
        seen.extend(leaf for leaf in _leaves(mapping) if not isinstance(leaf, str))
        with pytest.raises(TypeError):
            mapping["new"] = 1

    restrict(app, inject=[]).use(plugin).dispose()

    assert {type(leaf) for leaf in seen} == {FiberView, Hidden}
    for leaf in seen:
        with pytest.raises(CordisError):
            leaf.provide("leak", 1)  # type: ignore[attr-defined]
    assert root.get("leak") is None


def test_intercepts_and_intercept_layers_give_the_same_view_of_a_configuration(
    root: Context,
) -> None:
    host = root.use(lambda c, cfg: None)
    app = root.intercept("x", {"f": host})
    got: list[object] = []

    def plugin(ctx: Context, config: object) -> None:
        got.append(ctx.intercepts["x"])
        got.append(ctx.intercept_layers("x")[0])

    restrict(app, inject=[]).use(plugin)

    from_mapping, from_layers = (
        [type(leaf) for leaf in _leaves(exposed) if not isinstance(leaf, str)]
        for exposed in got
    )
    assert from_mapping == from_layers == [FiberView]


def test_expose_gives_a_placeholder_for_a_provider_and_a_method_of_a_fiber(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    impl = Impl(uid=1, name="svc", ctx=root, value=None, isolate=object())

    hidden_impl = expose(restricted, impl)
    hidden_method = expose(restricted, root.fiber.dispose)

    assert type(hidden_impl) is Hidden and type(hidden_method) is Hidden
    assert expose(root, impl) is impl
    with pytest.raises(CordisError, match="hidden Impl"):
        _ = hidden_impl.value
    with pytest.raises(CordisError, match="hidden method of Fiber"):
        hidden_method()
    with pytest.raises(CordisError, match="hidden Impl"):
        iter(hidden_impl)
    with pytest.raises(CordisError, match="hidden Impl"):
        hidden_impl.value = 1
    assert repr(hidden_impl) == "Hidden <Impl>"
    # protocol names work, so a listener can copy or record its arguments
    assert isinstance(hidden_impl, Hidden)
    assert not hasattr(hidden_impl, "__fspath__")


def test_expose_hides_the_method_of_every_context(root: Context) -> None:
    exposed: dict[str, object] = {}

    def plugin(ctx: Context, config: object) -> None:
        exposed["own"] = expose(ctx, ctx.provide)
        exposed["host"] = expose(ctx, root.provide)

    restrict(root, inject=["other"]).use(plugin)

    for hidden in exposed.values():
        assert type(hidden) is Hidden
        with pytest.raises(CordisError, match="hidden method of Context"):
            hidden()  # type: ignore[operator]


def test_expose_returns_an_object_that_it_does_not_recognize_unchanged(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    plain = _Plain()

    assert expose(restricted, plain) is plain
    assert expose(restricted, "text") == "text"


def _host_function() -> None:
    return None


@pytest.mark.parametrize(
    "build",
    [
        lambda root: root.fiber,
        lambda root: root,
        lambda root: Impl(uid=1, name="svc", ctx=root, value=None, isolate=object()),
        lambda root: root.provide,
        lambda root: _host_function,
    ],
    ids=["fiber", "foreign context", "provider record", "method", "function"],
)
def test_receive_gives_back_the_real_object_that_expose_hid_or_wrapped(
    root: Context, build: Callable[[Context], object]
) -> None:
    restricted = restrict(root, inject=["other"])
    value = build(root)

    exposed = expose(restricted, value)

    assert exposed is not value
    assert receive(restricted, exposed) is value
    assert receive(root, exposed) is exposed  # an unrestricted scope changes nothing


def test_receive_gives_back_each_element_of_a_container_and_a_service_view(
    root: Context,
) -> None:
    class Holder:
        cordis_tracker = Tracker(property="ctx")

        def __init__(self, ctx: Context) -> None:
            self.ctx = ctx

    restricted = restrict(root, inject=["other"])
    holder = Holder(root)
    view = expose(restricted, get_traceable(root, holder))

    assert view is not holder and isinstance(view, Holder)
    assert receive(restricted, view) is holder
    for build in (
        lambda: {root.fiber: [root.fiber, root]},
        lambda: MappingProxyType({root.fiber: [root.fiber, root]}),
    ):
        exposed = expose(restricted, build())
        received = receive(restricted, exposed)
        assert type(received) is type(exposed) and received == build()
        assert received is not exposed


def test_a_function_that_crosses_equals_its_function_and_has_no_weak_reference(
    root: Context,
) -> None:
    async def host_async() -> None:
        return None

    restricted = restrict(root, inject=["other"])
    wrapped = expose(restricted, _host_function)
    wrapped_async = expose(restricted, host_async)
    to_host = receive(restricted, _host_function)

    for crossing in (wrapped, to_host):
        assert crossing is not _host_function and crossing == _host_function
        assert hash(crossing) == hash(_host_function)
        assert {_host_function: 1}[crossing] == 1
        with pytest.raises(TypeError):
            weakref.ref(crossing)
    assert not inspect.iscoroutinefunction(wrapped)
    assert inspect.iscoroutinefunction(wrapped_async)
    # a function crosses back as itself in either direction
    assert expose(restricted, to_host) is _host_function
    assert expose(restricted, wrapped) is wrapped


def test_a_function_that_crosses_carries_its_arguments_and_its_result(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    view = expose(restricted, root.fiber)
    host_seen: list[object] = []
    restricted_seen: list[object] = []

    def host(fiber: object, *, key: object) -> object:
        host_seen.extend([fiber, key])
        return fiber

    def inside(fiber: object) -> object:
        restricted_seen.append(fiber)
        return fiber

    # a restricted caller of a host function: arguments received, result exposed
    result = expose(restricted, host)(view, key=view)
    assert host_seen == [root.fiber, root.fiber] and result == view
    assert type(result) is FiberView
    # the host caller of a restricted function: arguments exposed, result received
    assert receive(restricted, inside)(root.fiber) is root.fiber
    assert [type(seen) for seen in restricted_seen] == [FiberView]


def test_a_restricted_listener_gets_exposed_arguments_and_its_result_is_received(
    root: Context,
) -> None:
    seen: list[object] = []

    def listener(next_: Callable[[], object]) -> object:
        seen.append(next_())
        return seen[0]

    ContextView(restrict(root, inject=["other"])).on("wrap", listener)

    assert root.waterfall("wrap", lambda: root.fiber) is root.fiber
    assert [type(value) for value in seen] == [FiberView]


@pytest.mark.parametrize(
    "build",
    [
        lambda: functools.partial(_identity),
        lambda: [].append,
        lambda: lambda value: value,
    ],
    ids=["partial", "builtin method", "lambda"],
)
def test_every_function_form_crosses_as_a_function_that_equals_it(
    root: Context, build: Callable[[], Callable[..., object]]
) -> None:
    restricted = restrict(root, inject=["other"])
    function = build()

    exposed = expose(restricted, function)
    received = receive(restricted, function)

    for crossing in (exposed, received):
        assert crossing is not function and crossing == function and callable(crossing)
    assert receive(restricted, exposed) is function


def _identity(value: object) -> object:
    return value


def test_receive_of_a_deferred_settles_with_the_received_value_or_the_same_error(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    view = expose(restricted, root.fiber)
    resolved, rejected = Deferred[object](), Deferred[object]()
    error = CordisError("failed")

    received = [receive(restricted, source) for source in (resolved, rejected)]
    resolved.resolve(view)
    rejected.reject(error)

    assert received[0].result() is root.fiber
    with pytest.raises(CordisError) as excinfo:
        received[1].result()
    assert excinfo.value is error


def test_a_hidden_copy_is_the_same_placeholder_and_a_pickled_one_cannot_be_received(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    impl = Impl(uid=1, name="svc", ctx=root, value=None, isolate=object())
    hidden = expose(restricted, impl)

    assert copy.copy(hidden) is hidden and copy.deepcopy(hidden) is hidden
    unpickled = pickle.loads(pickle.dumps(hidden))
    assert type(unpickled) is Hidden and repr(unpickled) == "Hidden <Impl>"
    with pytest.raises(CordisError, match="lost its object"):
        receive(restricted, unpickled)


@pytest.mark.parametrize("fails", [False, True], ids=["resolves", "rejects"])
def test_expose_of_a_deferred_settles_with_the_exposed_value_or_the_same_error(
    root: Context, fails: bool
) -> None:
    restricted = restrict(root, inject=["other"])
    source = Deferred[Fiber]()

    exposed = expose(restricted, source)
    assert exposed is not source and not exposed.done()
    error = CordisError("failed")
    source.reject(error) if fails else source.resolve(root.fiber)

    if fails:
        with pytest.raises(CordisError) as excinfo:
            exposed.result()
        assert excinfo.value is error
    else:
        assert isinstance(exposed.result(), FiberView)


class _CallableObject:
    """A callable object that is not a function form."""

    marker = "kept"

    def __call__(self) -> str:
        return "called"


async def _host_fiber_later(fiber: Fiber) -> Fiber:
    return fiber


def test_a_callable_object_and_an_async_function_keep_their_form(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    callable_object = _CallableObject()

    exposed = expose(restricted, _host_fiber_later)

    assert expose(restricted, callable_object) is callable_object
    assert receive(restricted, callable_object) is callable_object
    assert inspect.iscoroutinefunction(exposed) and exposed == _host_fiber_later
    assert inspect.iscoroutinefunction(receive(restricted, _host_fiber_later))


def test_expose_of_a_settled_deferred_is_done_at_once() -> None:
    restricted = restrict(Context(ImmediateScheduler()), inject=["other"])
    fiber = restricted._fiber

    exposed = expose(restricted, Deferred.resolved(fiber))

    assert exposed.done() and isinstance(exposed.result(), FiberView)
    assert receive(restricted, exposed).result() is fiber


def _item_pair(
    direction: str, root: Context, restricted: Context
) -> tuple[Callable[..., object], object, object, type, type]:
    """Return the crossing, the source item, the item to send, and their two types.

    A source of the `expose` direction yields host objects and takes a view back. A
    source of the `receive` direction yields views and takes a host object back.
    """
    view = expose(restricted, root.fiber)
    if direction == "expose":
        return expose, root.fiber, view, FiberView, Fiber
    return receive, view, root.fiber, Fiber, FiberView


def _generator_source(
    item: object, log: list[object]
) -> Generator[object, object, str]:
    try:
        log.append((yield item))
        log.append((yield item))
    except CordisError as error:
        log.append(f"thrown {error}")
        raise
    finally:
        log.append("closed")
    return "done"


async def _async_generator_source(
    item: object, log: list[object]
) -> AsyncGenerator[object, object]:
    try:
        log.append((yield item))
        log.append((yield item))
    except CordisError as error:
        log.append(f"thrown {error}")
        raise
    finally:
        log.append("closed")


async def _coroutine_source(item: object, fails: bool) -> object:
    if fails:
        raise CordisError("failed")
    return item


@pytest.mark.parametrize("direction", ["expose", "receive"])
@pytest.mark.parametrize("fails", [False, True], ids=["result", "error"])
def test_a_coroutine_gives_a_crossed_result_or_the_same_error(
    root: Context, direction: str, fails: bool
) -> None:
    restricted = restrict(root, inject=["other"])
    cross, item, _, crossed_type, _ = _item_pair(direction, root, restricted)

    task = start(root.scheduler, cross(restricted, _coroutine_source(item, fails)))

    if fails:
        with pytest.raises(CordisError, match="failed"):
            task.result()
    else:
        assert type(task.result()) is crossed_type


@pytest.mark.parametrize("direction", ["expose", "receive"])
def test_a_generator_crosses_items_sent_values_and_the_return_value(
    root: Context, direction: str
) -> None:
    restricted = restrict(root, inject=["other"])
    cross, item, back, crossed_type, source_type = _item_pair(
        direction, root, restricted
    )
    log: list[object] = []
    wrapper = cross(restricted, _generator_source(item, log))

    first = next(wrapper)
    second = wrapper.send(back)
    with pytest.raises(StopIteration) as stop:
        wrapper.send(back)

    assert type(first) is crossed_type and type(second) is crossed_type
    assert [type(sent) for sent in log[:2]] == [source_type, source_type]
    assert stop.value.value == "done" and log[2] == "closed"


@pytest.mark.parametrize("operation", ["throw", "close"])
def test_a_generator_forwards_throw_and_close_to_its_source(
    root: Context, operation: str
) -> None:
    restricted = restrict(root, inject=["other"])
    log: list[object] = []
    wrapper = expose(restricted, _generator_source(root.fiber, log))
    next(wrapper)

    if operation == "throw":
        with pytest.raises(CordisError, match="stop"):
            wrapper.throw(CordisError("stop"))
        assert log == ["thrown stop", "closed"]
    else:
        wrapper.close()
        assert log == ["closed"]


@pytest.mark.parametrize("direction", ["expose", "receive"])
def test_an_async_generator_crosses_items_and_sent_values(
    root: Context, direction: str
) -> None:
    restricted = restrict(root, inject=["other"])
    cross, item, back, crossed_type, source_type = _item_pair(
        direction, root, restricted
    )
    log: list[object] = []

    async def consume() -> list[object]:
        wrapper = cross(restricted, _async_generator_source(item, log))
        items = [await wrapper.asend(None), await wrapper.asend(back)]
        with pytest.raises(StopAsyncIteration):
            await wrapper.asend(back)
        return items

    items = start(root.scheduler, consume()).result()

    assert [type(crossed) for crossed in items] == [crossed_type, crossed_type]
    assert [type(sent) for sent in log[:2]] == [source_type, source_type]
    assert log[2] == "closed"


@pytest.mark.parametrize("operation", ["athrow", "aclose"])
def test_an_async_generator_forwards_athrow_and_aclose_to_its_source(
    root: Context, operation: str
) -> None:
    restricted = restrict(root, inject=["other"])
    log: list[object] = []

    async def consume() -> None:
        wrapper = expose(restricted, _async_generator_source(root.fiber, log))
        await wrapper.asend(None)
        if operation == "athrow":
            with pytest.raises(CordisError, match="stop"):
                await wrapper.athrow(CordisError("stop"))
        else:
            await wrapper.aclose()

    start(root.scheduler, consume()).result()

    assert log == (["thrown stop", "closed"] if operation == "athrow" else ["closed"])


def test_a_bound_method_of_a_marked_object_crosses_its_arguments_and_result(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    marked = _Marked(root.fiber)

    result = expose(restricted, marked.fibers)()

    assert expose(restricted, marked.fibers) == marked.fibers
    assert _is_view(result) and result == expose(restricted, [root.fiber])


def test_an_exposed_view_forwards_the_protocol_operations_and_filters_them(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    foreign = Context(ImmediateScheduler())
    target = _Sequence([root.fiber, 3, foreign])
    view = expose(restricted, target)

    assert len(view) == 3 and bool(view) and not expose(restricted, _Sequence([]))
    assert type(view[0]) is FiberView and view[1] == 3
    assert expose(restricted, root.fiber) in view and 4 not in view
    assert view == expose(restricted, target) and view != 3
    assert hash(view) == hash(expose(restricted, target))
    assert [type(item) for item in expose(restricted, _Sequence([root.fiber]))] == [
        FiberView
    ]
    with pytest.raises(CordisError, match="hidden Context"):
        _ = view[2]
    with pytest.raises(CordisError, match="hidden Context"):
        list(view)


class _Sequence:
    """A marked object with the protocol operations."""

    cordis_expose = Exposure()

    def __init__(self, items: list[object]) -> None:
        self.items = items

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[object]:
        return iter(self.items)

    def __getitem__(self, index: int) -> object:
        return self.items[index]

    def __contains__(self, item: object) -> bool:
        return item in self.items


def test_a_restricted_once_listener_gets_exposed_arguments(root: Context) -> None:
    received: list[object] = []
    ContextView(restrict(root, inject=["other"])).once("greet", received.append)

    root.emit("greet", root.fiber)
    root.emit("greet", root.fiber)

    assert [type(item) for item in received] == [FiberView]


class _Probe(Service):
    """A service that hands out host objects through every read path."""

    cordis_expose = Exposure(hidden=frozenset({"secret"}), safe=frozenset({"raw"}))

    def __init__(self, ctx: Context, host: Fiber) -> None:
        super().__init__(ctx, "probe")
        self.host = host
        self.plain = host
        self.raw = host
        self.secret = 1
        self.stored = lambda: host
        self.host_ctx = ctx

    @property
    def prop(self) -> Fiber:
        return self.host

    def method(self) -> list[Fiber]:
        return [self.host]

    def hidden_result(self) -> Context:
        return self.host_ctx

    def own_fibers(self) -> list[Fiber]:
        """Read the fibers of the service itself, through `self`."""
        return [self.host, *self.method(), self.prop, self.plain]

    def same_host(self) -> bool:
        return self.method()[0] is self.host

    def cordis_invoke(self) -> Fiber:
        return self.host


class _PlainProbe(Service):
    """A service whose methods run with the view as `self` (`no_shadow`)."""

    def __init__(self, ctx: Context, host: Fiber) -> None:
        super().__init__(ctx, "plain_probe")
        self.cordis_tracker = Tracker(property="ctx", no_shadow=True)
        self.host = host
        self.host_ctx = ctx

    def method(self) -> list[Fiber]:
        return [self.host]

    def own_fibers(self) -> list[Fiber]:
        """Read the fibers of the service itself, through `self`."""
        return [self.host, *self.method()]

    def same_host(self) -> bool:
        return self.method()[0] is self.host

    def cordis_invoke(self) -> Context:
        return self.host_ctx


@pytest.fixture
def probe_scope(root: Context) -> tuple[Context, Context, Fiber]:
    host = root.fiber
    root.use(_Probe, host)
    root.use(_PlainProbe, host)
    root.accessor("probe.assoc", lambda ctx, receiver: host)
    restricted = restrict(root, inject=["probe", "probe.assoc", "plain_probe"])
    return root, restricted, host


def _is_view(value: object) -> bool:
    return type(value) is FiberView or (
        isinstance(value, list) and all(type(item) is FiberView for item in value)
    )


@pytest.mark.divergence("context-restriction")
@pytest.mark.parametrize(
    "read",
    [
        lambda probe, plain: probe.prop,
        lambda probe, plain: probe.plain,
        lambda probe, plain: probe.stored(),
        lambda probe, plain: probe.assoc,
        lambda probe, plain: probe.method(),
        lambda probe, plain: probe(),
        lambda probe, plain: plain.method(),
    ],
    ids=[
        "property",
        "attribute",
        "stored callable",
        "associated",
        "method",
        "call",
        "no_shadow method",
    ],
)
def test_a_service_view_filters_each_read_path_for_a_restricted_caller(
    probe_scope: tuple[Context, Context, Fiber],
    read: Callable[[object, object], object],
) -> None:
    root, restricted, host = probe_scope

    assert _is_view(read(restricted["probe"], restricted["plain_probe"]))
    # an unrestricted caller gets the real objects
    result = read(root["probe"], root["plain_probe"])
    assert result is host or result == [host]


@pytest.mark.divergence("context-restriction")
def test_a_service_view_hides_a_context_and_honors_hidden_and_safe_names(
    probe_scope: tuple[Context, Context, Fiber],
) -> None:
    root, restricted, host = probe_scope
    view = restricted["probe"]

    with pytest.raises(CordisError, match="hidden Context"):
        view.hidden_result()
    with pytest.raises(CordisError, match="hidden Context"):
        restricted["plain_probe"]()
    with pytest.raises(CordisError, match='cannot read "secret"'):
        _ = view.secret
    assert view.raw is host
    assert root["probe"].secret == 1
    # the service object itself reaches the scope as a filtered view
    traced = expose(restricted, root["probe"].cordis_original)
    with pytest.raises(CordisError, match='cannot read "secret"'):
        _ = traced.secret


_DIRECT = ["host_fiber", "host_list", "host_getter", "host_ctx"]


@pytest.mark.divergence("context-restriction")
@pytest.mark.parametrize(
    "read",
    [
        lambda ctx: ctx["host_fiber"],
        lambda ctx: ctx.get("host_fiber"),
        lambda ctx: ctx["host_list"],
        lambda ctx: ctx.get("host_list"),
        lambda ctx: ctx["host_getter"](),
        lambda ctx: ctx["host_acc"],
    ],
    ids=["item", "get", "list item", "list get", "provided callable", "accessor"],
)
def test_a_direct_read_filters_a_value_that_has_no_service_view(
    root: Context, read: Callable[[Context], object]
) -> None:
    host = root.use(_no_deps)
    root.provide("host_fiber", host)
    root.provide("host_list", [host])
    root.provide("host_getter", lambda: host)
    root.accessor("host_acc", lambda ctx, receiver: host)
    seen: list[object] = []

    def plugin(ctx: Context, config: object) -> None:
        seen.append(read(ctx))

    plugin.inject = _DIRECT[:3]
    fiber = restrict(root, inject=[*_DIRECT, "host_acc"]).use(plugin)

    assert fiber.state == FiberState.ACTIVE
    assert _is_view(seen[0])
    # an unrestricted caller gets the real objects
    result = read(root)
    assert result is host or result == [host]


@pytest.mark.divergence("context-restriction")
@pytest.mark.parametrize(
    ("read", "name"),
    [
        (lambda ctx: ctx["host_ctx"], "host_ctx"),
        (lambda ctx: ctx.get("host_ctx"), "get"),
    ],
    ids=["item", "get"],
)
def test_a_direct_read_of_a_host_context_fails_and_leaves_no_provider(
    root: Context, read: Callable[[Context], Context], name: str
) -> None:
    root.provide("host_ctx", root)

    def plugin(ctx: Context, config: object) -> None:
        read(ctx).provide("leak", 1)

    plugin.inject = ["host_ctx"]
    view = restrict(root, inject=["host_ctx"]).use(plugin)

    assert view.state == FiberState.FAILED
    assert f'"{name}" gives a hidden Context' in str(view.error)
    view.dispose()
    assert root.get("leak") is None
    assert read(root) is root


@pytest.mark.parametrize(
    ("name", "count"), [("probe", 4), ("plain_probe", 2)], ids=["shadowed", "no_shadow"]
)
def test_a_service_method_reads_real_objects_when_a_restricted_plugin_calls_it(
    probe_scope: tuple[Context, Context, Fiber], name: str, count: int
) -> None:
    root, restricted, host = probe_scope

    # the method reads `self.host` and `self.method()` on the real objects, and only
    # the list that it returns is filtered
    result = restricted[name].own_fibers()

    assert restricted[name].same_host() is True
    assert len(result) == count and all(type(item) is FiberView for item in result)
    assert all(receive(restricted, item) is host for item in result)


class _Marked:
    """A plain object with an `Exposure` and no `Tracker`."""

    cordis_expose = Exposure(hidden=frozenset({"token"}))

    def __init__(self, fiber: Fiber) -> None:
        self.fiber = fiber
        self.token = "t"
        self.name = "marked"

    def fibers(self) -> list[Fiber]:
        return [self.fiber]

    def combined(self) -> list[Fiber]:
        return [*self.fibers(), self.fiber]


def test_a_marked_object_reaches_a_restricted_scope_as_a_read_only_view(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    marked = _Marked(root.fiber)

    view = expose(restricted, marked)

    assert view is not marked and isinstance(view, _Marked)
    assert view.cordis_original is marked
    assert view.name == "marked"
    assert type(view.fiber) is FiberView
    # a method runs on the object itself, and its result is filtered
    assert _is_view(view.fibers()) and len(view.combined()) == 2
    with pytest.raises(CordisError, match='cannot read "token"'):
        _ = view.token
    with pytest.raises(CordisError, match="cannot set"):
        view.name = "other"
    with pytest.raises(CordisError, match="cannot delete"):
        del view.name
    assert expose(root, marked) is marked
    assert expose(restricted, [marked])[0].cordis_original is marked


@pytest.fixture
def loader_scope(
    root: Context, loader: RecordingLoader
) -> tuple[Context, RecordingLoader]:
    """A loader with an entry `a` that has an outer fiber, and a restricted scope."""
    root.provide("dep", {})
    loader.mock("a", inject=None)
    loader.read([{"id": "a", "name": "a", "inject": ["dep"]}]).result()
    return restrict(root, inject=["loader"]), loader


@pytest.mark.divergence("context-restriction")
@pytest.mark.parametrize(
    "read",
    [
        lambda entry: entry.live_fiber(),
        lambda entry: entry.inner_fiber,
        lambda entry: entry.outer_fiber,
    ],
    ids=["live_fiber", "inner_fiber", "outer_fiber"],
)
def test_a_loader_entry_gives_a_restricted_caller_fiber_views(
    loader_scope: tuple[Context, RecordingLoader],
    read: Callable[[object], object],
) -> None:
    restricted, loader = loader_scope
    entry = restricted["loader"].resolve("a")

    assert type(read(entry)) is FiberView
    assert type(read(loader.resolve("a"))) is Fiber


def _provider_of(loader: RecordingLoader, name: str) -> object:
    return loader.ctx.get(name, strict=False)


@pytest.mark.divergence("context-restriction")
@pytest.mark.parametrize(
    "escape",
    [
        lambda entry: entry.live_fiber().ctx,
        lambda entry: entry.outer_fiber.ctx,
        lambda entry: entry.parent.ctx,
        lambda entry: entry.parent.tree.ctx.provide("x", 1),
    ],
    ids=["live_fiber.ctx", "outer_fiber.ctx", "group.ctx", "tree.ctx.provide"],
)
def test_a_loader_entry_gives_a_restricted_caller_no_host_context(
    loader_scope: tuple[Context, RecordingLoader],
    escape: Callable[[object], object],
) -> None:
    restricted, loader = loader_scope
    entry = restricted["loader"].resolve("a")

    with pytest.raises(CordisError):
        escape(entry)
    assert _provider_of(loader, "x") is None


@pytest.mark.divergence("context-restriction")
def test_a_restricted_caller_still_reads_entry_ids_names_and_states(
    loader_scope: tuple[Context, RecordingLoader],
) -> None:
    restricted, _ = loader_scope
    entry = restricted["loader"].resolve("a")

    assert entry.id == "a" and entry.options["name"] == "a"
    assert entry.live_fiber().state == FiberState.ACTIVE
    assert [item.id for item in restricted["loader"].entries()] == ["a"]


def test_loader_entry_of_and_locate_accept_a_fiber_view(
    loader_scope: tuple[Context, RecordingLoader],
) -> None:
    restricted, loader = loader_scope
    fiber = loader.fiber("a")
    view = expose(restricted, fiber)

    assert type(view) is FiberView
    assert restricted["loader"].entry_of(view).id == "a"
    assert restricted["loader"].locate(view) == "a"
    assert loader.entry_of(fiber) is loader.resolve("a")
    assert loader.locate(fiber) == "a"


def test_a_restricted_plugin_logs_but_cannot_read_the_log_of_the_root(
    root: Context,
    caplog: pytest.LogCaptureFixture,
) -> None:
    restricted = restrict(root, inject=["other"])
    seen: dict[str, object] = {}

    def component(ctx: Context, config: object) -> None:
        ctx.logger("x").warning("named")
        ctx.logger.warning("direct")
        seen["extra"] = ctx.logger("x")
        seen["service"] = ctx.logger

    with caplog.at_level(logging.WARNING, logger="pycordis"):
        fiber = restricted.use(component)

    assert fiber.state == FiberState.ACTIVE
    records = [r for r in root.logger.buffer if r.getMessage() in ("named", "direct")]
    assert len(records) == 2
    assert all(type(record.cordis_fiber) is Fiber for record in records)
    with pytest.raises(CordisError, match='cannot read "extra"'):
        _ = seen["extra"].extra
    for name in ("buffer", "exporter"):
        with pytest.raises(CordisError, match=f'cannot read "{name}"'):
            getattr(seen["service"], name)
    root.logger.exporter(logging.NullHandler())()


@pytest.mark.divergence("context-restriction")
def test_a_reload_result_gives_a_restricted_caller_fiber_views(
    root: Context, tmp_path: Path
) -> None:
    service = root.use(Hmr, HmrConfig(scope=(str(tmp_path),)))
    restricted = restrict(root, inject=["hmr"])
    fiber = root.fiber

    try:
        result = restricted["hmr"].reconcile(lambda: []).result()
        exposed = expose(restricted, ReloadResult(settled=[fiber]))
    finally:
        service.dispose()  # only one `Hmr` can be active in a process

    assert isinstance(result, ReloadResult) and result.settled == []
    assert isinstance(exposed, ReloadResult)
    assert [type(item) for item in exposed.settled] == [FiberView]
    assert receive(restricted, exposed.settled[0]) is fiber


class TestContextView:
    """`ContextView`: the member table and the crossing of each kind."""

    @staticmethod
    def _view(root: Context) -> tuple[ContextView, Context]:
        """A view of the context of a restricted component, and that context."""
        held: list[Context] = []
        restrict(root, inject=["other", "mine"]).use(
            lambda ctx, config: held.append(real_context(ctx))
        )
        return ContextView(held[0]), held[0]

    def test_the_member_table_has_exactly_the_public_members_of_context(
        self, root: Context
    ) -> None:
        public = {name for name in dir(Context) if not name.startswith("_")}
        public |= {name for name in vars(root) if not name.startswith("_")}

        assert set(ContextView.members) == public

    @pytest.mark.parametrize(
        "name",
        [
            "root",
            "reflect",
            "registry",
            "events",
            "intercept_layer",
            "intercept_through",
            "bind_fiber",
            "with_shadow",
            "not_a_member",
        ],
    )
    def test_a_guarded_or_unknown_name_raises(self, root: Context, name: str) -> None:
        view, _ = self._view(root)

        with pytest.raises(CordisError, match=f'cannot read "{name}"'):
            getattr(view, name)

    def test_a_write_or_delete_of_an_attribute_raises(self, root: Context) -> None:
        view, _ = self._view(root)

        with pytest.raises(CordisError, match='cannot set "meta"'):
            view.meta = {}  # type: ignore[misc]
        with pytest.raises(CordisError, match='cannot delete "meta"'):
            del view.meta  # type: ignore[misc]

    def test_a_pass_member_gives_the_member_of_the_context(self, root: Context) -> None:
        view, ctx = self._view(root)

        assert view.scheduler is ctx.scheduler
        assert view.isolates == ctx.isolates

    @pytest.mark.parametrize(
        "member", ["emit", "bail", "serial", "parallel", "waterfall"]
    )
    @pytest.mark.parametrize("how", ["positional", "keyword"])
    def test_a_dispatch_of_an_internal_event_raises(
        self, root: Context, member: str, how: str
    ) -> None:
        view, _ = self._view(root)
        call = getattr(view, member)

        with pytest.raises(CordisError, match='cannot dispatch "internal/plugin"'):
            if how == "positional":
                call("internal/plugin")
            else:
                call(name="internal/plugin")

    def test_a_listener_gets_exposed_arguments_and_its_result_is_received(
        self, root: Context
    ) -> None:
        view, ctx = self._view(root)
        seen: list[object] = []

        def listener(fiber: object, *rest: object) -> object:
            seen.append(fiber)
            return fiber

        view.on("probe", listener)

        result = view.bail("probe", root.fiber)

        assert type(result) is FiberView and receive(ctx, result) is root.fiber
        assert type(seen[0]) is FiberView
        with pytest.raises(CordisError):
            seen[0].dispose()  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "derive",
        [
            lambda view: view.extend(key=1),
            lambda view: view.isolate("svc"),
            lambda view: view.intercept("svc", {}),
        ],
        ids=["extend", "isolate", "intercept"],
    )
    def test_a_derived_context_is_a_view(
        self, root: Context, derive: Callable[[ContextView], object]
    ) -> None:
        view, ctx = self._view(root)

        derived = derive(view)

        assert type(derived) is ContextView
        assert real_context(derived) is not ctx
        assert real_context(derived)._restrictions == ctx._restrictions

    def test_a_property_read_gives_an_exposed_value(self, root: Context) -> None:
        view, ctx = self._view(root)

        assert type(view.fiber) is FiberView
        assert view.extend(key=1).meta["key"] == 1
        assert view.intercept("svc", "cfg").intercepts["svc"] == "cfg"

    def test_an_effect_of_a_view_reverts_with_its_fiber(self, root: Context) -> None:
        view, ctx = self._view(root)
        log: list[str] = []

        view.effect(lambda: lambda: log.append("reverted"))
        ctx.fiber.dispose()

        assert log == ["reverted"]

    def test_an_accessor_of_a_view_gets_a_hidden_host_context(
        self, root: Context
    ) -> None:
        view, ctx = self._view(root)
        seen: list[object] = []
        view.accessor("probe", lambda reader, receiver: seen.append(reader) or 1)

        assert root["probe"] == 1
        assert type(seen[0]) is Hidden  # the host context is not the component's

    def test_use_gives_a_fiber_view_and_passes_the_component_unchanged(
        self, root: Context
    ) -> None:
        view, _ = self._view(root)
        got: list[object] = []

        def component(ctx: Context, config: object) -> None:
            got.append(config)

        fiber = view.use(component, {"a": 1})

        assert type(fiber) is FiberView and fiber.state == FiberState.ACTIVE
        assert got == [{"a": 1}]
        assert fiber.name == "component"  # a wrapper would not carry the name

    def test_item_access_crosses_in_both_directions(self, root: Context) -> None:
        view, ctx = self._view(root)
        ctx.provide("mine", {"fiber": ctx.fiber})

        assert type(view["mine"]["fiber"]) is FiberView
        view["mine"] = view.fiber  # a FiberView crosses as the real fiber
        assert root["mine"] is receive(ctx, ctx.fiber)

    def test_a_context_that_the_component_does_not_own_is_hidden(
        self, root: Context
    ) -> None:
        view, _ = self._view(root)
        root.provide("other", root)

        with pytest.raises(CordisError, match="hidden Context"):
            view["other"]

    def test_restrict_of_a_view_gives_a_view_that_narrows(self, root: Context) -> None:
        view, ctx = self._view(root)

        narrowed = restrict(view, inject=[])

        assert type(narrowed) is ContextView
        assert real_context(narrowed)._restrictions[: len(ctx._restrictions)] == (
            ctx._restrictions
        )
        assert len(real_context(narrowed)._restrictions) == len(ctx._restrictions) + 1

    def test_views_equal_by_context_and_receive_gives_the_context(
        self, root: Context
    ) -> None:
        view, ctx = self._view(root)

        assert view == ContextView(ctx) and hash(view) == hash(ContextView(ctx))
        assert view != ContextView(root)
        assert not isinstance(view, Context)
        assert receive(ctx, view) is ctx
        assert real_context(ctx) is ctx and real_context(view) is ctx
        assert copy.deepcopy(view) is view


class TestComponentHoldsView:
    """A restricted component, `inject` callback, and `@inject` method get a view."""

    def test_each_form_of_component_gets_a_context_view(self, root: Context) -> None:
        held: dict[str, object] = {}

        def function(ctx: object, config: object) -> None:
            held["function"] = ctx

        class Cls:
            def __init__(self, ctx: object, config: object) -> None:
                held["class"] = ctx

        class WithMethod:
            def __init__(self, ctx: object, config: object) -> None:
                self.ctx = ctx

            @inject_method("other")
            def method(self) -> None:
                held["method"] = self.ctx

        class WithApply:
            def apply(self, ctx: object, config: object) -> None:
                held["apply"] = ctx

        restricted = restrict(root, inject=["other"])
        root.provide("other", object())
        restricted.use(function)
        restricted.use(Cls)
        restricted.use(WithMethod)
        restricted.use(WithApply())
        restricted.inject(["other"], lambda ctx: held.__setitem__("callback", ctx))

        assert set(held) == {"function", "class", "method", "apply", "callback"}
        assert all(type(ctx) is ContextView for ctx in held.values())

    def test_a_component_that_the_host_also_uses_gets_the_real_context(
        self, root: Context
    ) -> None:
        held: list[object] = []
        restricted = restrict(root, inject=["other"])

        def component(ctx: object, config: object) -> None:
            held.append(ctx)

        restricted.use(component)
        root.use(component)

        assert [type(ctx) for ctx in held] == [ContextView, Context]

    def test_a_restricted_service_builds_with_a_view_and_the_host_reads_it(
        self, root: Context
    ) -> None:
        held: list[object] = []

        class Greeter(Service):
            provide = "greeter"

            def __init__(self, ctx: object, config: object = None) -> None:
                super().__init__(ctx)  # type: ignore[arg-type]
                held.append(ctx)

            def hello(self) -> str:
                return "hello"

        fiber = restrict(root, provide=["greeter"]).use(Greeter)

        assert fiber.state == FiberState.ACTIVE
        assert type(held[0]) is ContextView
        assert root["greeter"].hello() == "hello"

    @pytest.mark.parametrize("member", ["use", "inject", "effect"])
    def test_the_call_site_of_a_failure_names_the_line_in_the_component(
        self, root: Context, member: str
    ) -> None:
        def failing(*args: object) -> None:
            raise RuntimeError("boom")

        arguments = {"use": (failing,), "inject": ([], failing), "effect": (failing,)}

        def component(ctx: ContextView, config: object) -> None:
            getattr(ctx, member)(*arguments[member])  # the recorded line

        fiber = restrict(root, inject=["other"]).use(component)
        notes = [
            note
            for f in (fiber, *_all_fibers(fiber))
            if isinstance(f.error, RuntimeError)
            for note in f.error.__notes__
        ]

        assert any(note.splitlines()[-1].endswith("component") for note in notes)
        assert not any("_cross_member" in note for note in notes)


def _all_fibers(fiber: Fiber) -> Iterator[Fiber]:
    for child in fiber.children:
        yield child
        yield from _all_fibers(child)


class _Vault(Service):
    """A service that a restricted plugin provides."""

    provide = "vault"

    def __init__(self, ctx: object, config: object = None) -> None:
        super().__init__(ctx, "vault")  # type: ignore[arg-type]
        self.seen: list[object] = []
        self.callbacks: list[Callable[..., object]] = []

    def register(self, item: object) -> object:
        self.seen.append(item)
        return item

    def context_type(self) -> type:
        return type(self.ctx)

    def subscribe(self, callback: Callable[..., object]) -> None:
        self.callbacks.append(callback)

    def unsubscribe(self, callback: Callable[..., object]) -> None:
        self.callbacks.remove(callback)

    def fire(self, item: object) -> None:
        for callback in self.callbacks:
            callback(item)

    def cordis_invoke(self, *items: object, **named: object) -> type:
        self.seen.extend([*items, *named.values()])
        return type(self.ctx)


class _PlainVault(_Vault):
    """A restricted service whose methods run with the view as `self`."""

    def __init__(self, ctx: object, config: object = None) -> None:
        super().__init__(ctx, config)
        self.cordis_tracker = Tracker(property="ctx", no_shadow=True)


class _HostEcho(Service):
    """A callable host service that returns its arguments."""

    provide = "echo"

    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "echo")
        self.received: list[object] = []

    def cordis_invoke(self, *items: object, **named: object) -> list[object]:
        self.received.extend([*items, *named.values()])
        return [*items, *named.values()]


class _HostBus(Service):
    """A host service that stores the callbacks that it gets."""

    provide = "bus"

    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "bus")
        self.callbacks: list[Callable[..., object]] = []

    def subscribe(self, callback: Callable[..., object]) -> None:
        self.callbacks.append(callback)

    def unsubscribe(self, callback: Callable[..., object]) -> None:
        self.callbacks.remove(callback)

    def fire(self, *args: object, **kwargs: object) -> list[object]:
        return [callback(*args, **kwargs) for callback in self.callbacks]


class TestServiceDirections:
    """A service view crosses in the direction that its restricted side decides."""

    @pytest.mark.divergence("context-restriction")
    @pytest.mark.parametrize(
        "read",
        [
            lambda vault: vault.context_type(),
            lambda vault: vault(),
        ],
        ids=["method", "cordis_invoke"],
    )
    @pytest.mark.parametrize("service", [_Vault, _PlainVault])
    def test_a_method_of_a_restricted_service_gets_a_context_view(
        self,
        root: Context,
        service: type[_Vault],
        read: Callable[[_Vault], type],
    ) -> None:
        restrict(root, provide=["vault"]).use(service)

        assert read(root["vault"]) is ContextView

    @pytest.mark.divergence("context-restriction")
    def test_a_host_reader_exposes_its_arguments_and_receives_the_results(
        self, root: Context
    ) -> None:
        restrict(root, provide=["vault"]).use(_Vault)
        vault = root["vault"]

        result = vault.register(root.fiber)

        assert result is root.fiber
        (seen,) = vault.cordis_original.seen
        assert type(seen) is FiberView

    @pytest.mark.divergence("context-restriction")
    def test_a_restricted_reader_of_a_host_service_gives_callbacks_both_ways(
        self, root: Context
    ) -> None:
        root.use(_HostBus)
        received: list[object] = []

        def callback(item: object) -> None:
            received.append(item)

        def component(ctx: ContextView, config: object) -> None:
            bus = ctx["bus"]
            bus.subscribe(callback)
            bus.fire(ctx.fiber)  # the callback is called with a view
            bus.unsubscribe(callback)  # a wrapper equals its function
            bus.fire(item=ctx.fiber)

        component.inject = ["bus"]  # type: ignore[attr-defined]
        restrict(root, inject=["bus"]).use(component)

        assert [type(item) for item in received] == [FiberView]
        assert root["bus"].callbacks == []

    @pytest.mark.divergence("context-restriction")
    def test_a_restricted_reader_gets_its_own_context_as_the_caller(
        self, root: Context
    ) -> None:
        root.use(_HostBus)
        held: dict[str, object] = {}

        def component(ctx: ContextView, config: object) -> None:
            held["own"] = ctx
            held["caller"] = ctx["bus"].cordis_caller
            try:
                _ = ctx["bus"].cordis_caller.root
            except CordisError as error:
                held["error"] = error

        component.inject = ["bus"]  # type: ignore[attr-defined]
        restrict(root, inject=["bus"]).use(component)

        assert held["caller"] == held["own"]
        assert 'cannot read "root"' in str(held["error"])

    @pytest.mark.contract("EFF-ISOLATE")
    @pytest.mark.divergence("context-restriction")
    def test_a_restricted_service_as_this_reaches_only_the_listeners_of_its_realm(
        self, root: Context
    ) -> None:
        restrict(root, provide=["vault"]).use(_Vault)
        calls: list[str] = []
        root.on("ping", lambda: calls.append("root"))
        root.isolate("vault").on("ping", lambda: calls.append("isolated"))

        root.emit("ping", this=root["vault"])

        assert calls == ["root"]

    @pytest.mark.divergence("context-restriction")
    def test_a_restricted_filter_gets_realm_views(self, root: Context) -> None:
        seen: list[object] = []
        errors: list[CordisError] = []

        class Filter:
            def cordis_filter(self, ctx: object) -> bool:
                seen.append(ctx.isolates)  # type: ignore[attr-defined]
                try:
                    _ = ctx.provide  # type: ignore[attr-defined]
                except CordisError as error:
                    errors.append(error)
                return True

        def component(ctx: ContextView, config: object) -> None:
            ctx.emit("ping", this=Filter())

        root.on("ping", lambda: None)
        restrict(root, inject=["x"]).use(component)

        assert seen == [root.isolates]
        assert 'cannot read "provide" of a realm view' in str(errors[0])
        assert type(realm_filter(root, Filter())) is Filter  # unrestricted: unchanged

    @pytest.mark.divergence("context-restriction")
    def test_two_restricted_scopes_compose(self, root: Context) -> None:
        restrict(root, provide=["vault"]).use(_Vault)
        held: dict[str, object] = {}

        def component(ctx: ContextView, config: object) -> None:
            own = ctx.fiber
            held["own"] = own
            held["result"] = ctx["vault"].register(own)

        component.inject = ["vault"]  # type: ignore[attr-defined]
        restrict(root, inject=["vault"]).use(component)

        (seen,) = root["vault"].cordis_original.seen
        assert type(seen) is FiberView and seen != held["own"]  # one view per scope
        assert held["result"] == held["own"]

    @pytest.mark.divergence("context-restriction")
    def test_a_call_of_a_restricted_service_exposes_positional_and_keyword_arguments(
        self, root: Context
    ) -> None:
        restrict(root, provide=["vault"]).use(_Vault)
        vault = root["vault"]

        vault(root.fiber, item=root.fiber)

        assert [type(item) for item in vault.cordis_original.seen] == [FiberView] * 2

    @pytest.mark.divergence("context-restriction")
    def test_a_call_of_a_host_service_receives_arguments_and_exposes_results(
        self, root: Context
    ) -> None:
        root.use(_HostEcho)
        held: list[object] = []

        def component(ctx: ContextView, config: object) -> None:
            held.extend(ctx["echo"](ctx.fiber, item=ctx.fiber))

        component.inject = ["echo"]  # type: ignore[attr-defined]
        fiber = restrict(root, inject=["echo"]).use(component)

        assert [type(item) for item in root["echo"].received] == [Fiber] * 2
        assert [type(item) for item in held] == [FiberView] * 2
        assert held[0].name == fiber.name  # type: ignore[attr-defined]

    @pytest.mark.divergence("context-restriction")
    @pytest.mark.parametrize(
        "action",
        [
            lambda view: setattr(view, "isolates", {}),
            lambda view: delattr(view, "isolates"),
            lambda view: view.provide,
            lambda view: view.fiber,
        ],
        ids=["set", "delete", "provide", "fiber"],
    )
    def test_a_realm_view_gives_isolates_and_refuses_everything_else(
        self, root: Context, action: Callable[[RealmView], object]
    ) -> None:
        view = RealmView(root)

        assert view.isolates == root.isolates
        with pytest.raises(CordisError, match="realm view"):
            action(view)

    def test_realm_filter_keeps_a_this_without_a_filter(self, root: Context) -> None:
        scope = restrict(root, inject=["x"])
        this = object()

        assert realm_filter(scope, this) is this
