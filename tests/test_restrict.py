"""`restrict`: which inject, read, and provide keys a derived context admits."""

from collections.abc import Callable

import pytest

from pycordis.context import Context, Rule, restrict
from pycordis.errors import CordisError
from pycordis.fiber import Fiber, FiberState, FiberView
from pycordis.loader import Loader
from pycordis.registry import inject as inject_method
from pycordis.scheduler import start


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
            restrict(root, inject=["other"])
            .inject(["other"], lambda ctx: None)
            .unwrap(root)
            .ctx
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
def test_a_guarded_attribute_raises_in_a_restricted_context_and_works_in_others(
    root: Context, read: Callable[[Context], object]
) -> None:
    value = read(root)
    assert value is read(root)

    restricted = restrict(root, inject=["other"])
    with pytest.raises(CordisError, match="restricted context"):
        read(restricted)


def test_a_guarded_attribute_is_checked_at_the_def_site(root: Context) -> None:
    unrestricted_service = root.inject(["db"], lambda ctx: None).ctx
    restricted_caller = restrict(root, inject=["other"])

    # the def site (the service's own context) is unrestricted: the caller's
    # restriction does not apply
    shadowed = restricted_caller.with_shadow(unrestricted_service)
    assert shadowed.root is root

    # `fiber` follows the def site too: an unrestricted def site gives real fibers
    caller_fiber = restrict(root, inject=["other"]).use(_no_deps).unwrap(root)
    assert caller_fiber.ctx.with_shadow(unrestricted_service).fiber is caller_fiber
    assert shadowed.fiber is root.fiber

    restricted_service = (
        restrict(root, inject=["other"])
        .inject(["other"], lambda ctx: None)
        .unwrap(root)
        .ctx
    )

    # the def site is restricted even though the caller (root) has no restriction
    with pytest.raises(CordisError, match="restricted context"):
        _ = root.with_shadow(restricted_service).root


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
    view = restricted.fiber

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
    assert not hasattr(view, "__deepcopy__")


@pytest.mark.parametrize("fails", [False, True], ids=["resolves", "rejects"])
def test_wait_and_await_on_a_view_settle_with_the_view(
    root: Context, fails: bool
) -> None:
    view = restrict(root, inject=["other"]).use(_needs_db if fails else _no_deps)

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
    sibling = restricted.use(_no_deps).unwrap(root)
    host = root.use(_no_deps)
    seen: dict[str, FiberView] = {}

    def plugin(ctx: Context, config: object) -> None:
        seen["own"] = ctx.use(_no_deps)
        seen["grandchild"] = restrict(ctx, inject=["other"]).use(_no_deps)
        # views of fibers outside the subtree, for the scope of this plugin
        seen["sibling"] = ctx._view_of(sibling)
        seen["host"] = ctx._view_of(host)
        seen["root"] = ctx._view_of(root.fiber)

    assert restricted.use(plugin).state == FiberState.ACTIVE

    for name in ("own", "grandchild"):
        act(seen[name])
    for name in ("sibling", "host", "root"):
        with pytest.raises(CordisError, match="does not own"):
            act(seen[name])


def test_a_view_equals_a_view_of_the_same_fiber_for_the_same_scope(
    root: Context,
) -> None:
    restricted = restrict(root, inject=["other"])
    other_scope = restrict(root, inject=["other"])
    view = restricted.use(_no_deps)
    fiber = view.unwrap(root)

    assert restricted._view_of(fiber) == view
    assert hash(restricted._view_of(fiber)) == hash(view)
    assert view in {restricted._view_of(fiber)}
    assert view != fiber and fiber != view
    assert other_scope._view_of(fiber) != view


def test_unwrap_needs_an_unrestricted_def_site(root: Context) -> None:
    restricted = restrict(root, inject=["other"])
    view = restricted.use(_no_deps)
    restricted_ctx = restricted.use(_no_deps).unwrap(root).ctx

    assert view.unwrap(root) is root.fiber.children[0]
    assert view.unwrap(restricted_ctx.with_shadow(root)) is view.unwrap(root)
    with pytest.raises(CordisError, match="restricted context"):
        view.unwrap(restricted_ctx)
    with pytest.raises(CordisError, match="restricted context"):
        view.unwrap(root.with_shadow(restricted_ctx))


@pytest.mark.divergence("context-restriction")
def test_a_restricted_plugin_reading_loader_ctx_gets_the_use_site(
    root: Context,
) -> None:
    """Closes the `Loader` escape: `ctx['loader'].ctx` gives the use site, not the
    loader's own unrestricted context."""
    root.use(Loader)
    restricted = restrict(root, inject=["loader"])
    captured: dict[str, Context] = {}

    def component(ctx: Context, config: object) -> None:
        captured["own"] = ctx
        captured["loader_ctx"] = ctx["loader"].ctx

    component.inject = ["loader"]
    fiber = restricted.use(component)

    assert fiber.state == FiberState.ACTIVE
    assert captured["loader_ctx"] is captured["own"]


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
