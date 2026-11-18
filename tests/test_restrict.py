"""`restrict`: which inject, read, and provide keys a derived context admits."""

from collections.abc import Callable

import pytest

from pycordis.context import Context, Rule, restrict
from pycordis.fiber import Fiber, FiberState
from pycordis.registry import inject as inject_method


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

    def test_a_dimension_with_no_rule_admits_every_key(self, root: Context) -> None:
        restricted = restrict(root, inject=["other"])
        # the provide dimension has no rule: any name can be provided
        restricted.provide("anything", 1)
        assert restricted.get("anything") == 1

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


def _via_component(ctx: Context) -> Fiber:
    return ctx.use(_needs_db)


def _via_inject_method(ctx: Context) -> Fiber:
    class Owner:
        def __init__(self, ctx: Context, config: object) -> None:
            pass

        @inject_method("db")
        def warm(self) -> None:
            return None

    owner = ctx.use(Owner)
    return owner.children[0]


def _via_ctx_inject(ctx: Context) -> Fiber:
    return ctx.inject(["db"], lambda inner: None)


def _via_replace(ctx: Context) -> Fiber:
    fiber = ctx.use(_no_deps)
    ctx.registry.replace(_no_deps, _needs_db)
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
    root: Context, via: Callable[[Context], Fiber]
) -> None:
    restricted = restrict(root, inject=["other"])
    sibling = restricted.use(lambda ctx, config: None)

    fiber = via(restricted)

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
