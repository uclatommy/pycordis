"""Services, accessors, and realms of the root context."""

import random
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import Recorder

from pycordis.context import Context
from pycordis.errors import CordisError
from pycordis.fiber import FiberState
from pycordis.keys import Key
from pycordis.scheduler import ImmediateScheduler


@pytest.mark.upstream("reflect.spec.ts::Reflect::Context.is()")
def test_a_subclass_instance_is_a_context(scheduler: ImmediateScheduler) -> None:
    class SubContext(Context):
        pass

    assert isinstance(SubContext(scheduler), Context)


def test_a_provided_service_is_read_with_get_and_a_mixin(root: Context) -> None:
    root.mixin("foo", ["bar"])
    root.provide("foo")
    root.set("foo", SimpleNamespace(bar=1))

    assert root.get("foo") is not None
    assert root["bar"] == 1
    assert root.get("bar") is None


@pytest.mark.parametrize("spelling", ["foo", Key[int]("foo")])
def test_a_key_and_its_name_select_the_same_service(
    root: Context, spelling: Any
) -> None:
    root.provide(spelling, 1)

    assert root.get("foo") == root.get(Key[int]("foo")) == root[spelling] == 1
    root[spelling] = 2
    assert root.get("foo") == 2


@pytest.mark.parametrize("read", ["item", "get"])
@pytest.mark.parametrize("reader", ["root", "component"])
def test_a_key_must_be_a_str_or_a_key(root: Context, read: str, reader: str) -> None:
    ctx = root if reader == "root" else root.use(lambda ctx, config: None).ctx

    with pytest.raises(TypeError):
        ctx[1] if read == "item" else ctx.get(1)  # type: ignore[index, arg-type]


def test_a_service_that_nobody_provides_reads_as_none(root: Context) -> None:
    assert root["missing"] is None
    assert root.get("missing") is None


def test_set_without_a_provider_is_an_error(root: Context) -> None:
    with pytest.raises(CordisError, match='cannot set property "foo" without provide'):
        root.set("foo", 0)
    with pytest.raises(CordisError, match='cannot set property "foo" without provide'):
        root["foo"] = 0


def test_a_second_provider_in_one_realm_is_an_error(root: Context) -> None:
    root.provide("foo", 1)

    with pytest.raises(
        CordisError, match='service "foo" has been registered at <root>'
    ):
        root.provide("foo", 2)

    assert [effect.label for effect in root.fiber.get_effects()] == [
        'ctx.provide("foo")'
    ]
    assert len(root.fiber.provisions) == 1
    assert root.get("foo") == 1


@pytest.mark.parametrize(
    ("first", "second", "message"),
    [
        ("accessor", "provide", 'property "foo" is already declared as accessor'),
        ("provide", "accessor", 'property "foo" is already declared as service'),
        ("accessor", "accessor", 'property "foo" is already declared as accessor'),
    ],
)
def test_a_property_name_has_one_kind(
    root: Context, first: str, second: str, message: str
) -> None:
    def declare(kind: str) -> None:
        if kind == "provide":
            root.provide("foo")
        else:
            root.accessor("foo", lambda ctx, receiver: 1)

    declare(first)
    with pytest.raises(CordisError, match=message):
        declare(second)


def test_the_provider_value_is_visible_only_while_the_fiber_is_active(
    root: Context,
) -> None:
    seen: list[tuple[object, object, object]] = []
    root.provide("foo", 1)
    root.effect(
        lambda: (
            lambda: seen.append(
                (root.get("foo"), root.get("foo", strict=False), root["foo"])
            )
        )
    )

    root.fiber.dispose()

    assert seen == [(None, 1, 1)]


@pytest.mark.parametrize("on_removal", [False, True])
def test_a_provision_reports_its_change_to_service_listeners(
    root: Context, on_removal: bool
) -> None:
    heard = Recorder()
    root.on("internal/service", heard)
    handle = root.provide("foo", 7)
    if on_removal:
        handle()

    assert heard.calls == [("foo", 7)] + ([("foo", None)] if on_removal else [])


@pytest.mark.parametrize("label_shared", [False, True])
def test_contexts_that_share_a_realm_share_the_provider(
    root: Context, label_shared: bool
) -> None:
    first = root.isolate("foo", "realm")
    second = root.isolate("foo", "realm" if label_shared else None)
    first.provide("foo", 1)

    assert second.get("foo") == (1 if label_shared else None)
    assert root.get("foo") is None


def test_an_extended_context_sees_the_provider_of_its_parent(root: Context) -> None:
    root.provide("foo", 1)

    assert root.extend(tag=1).get("foo") == 1


def test_an_isolated_context_can_provide_beside_the_default_realm(
    root: Context,
) -> None:
    root.provide("foo", 1)
    isolated = root.isolate("foo")
    isolated.provide("foo", 2)

    assert (root.get("foo"), isolated.get("foo")) == (1, 2)


def test_service_listeners_hear_only_their_own_realm(root: Context) -> None:
    isolated = root.isolate("foo")
    default_heard, isolated_heard = Recorder(), Recorder()
    root.on("internal/service", default_heard)
    isolated.on("internal/service", isolated_heard)

    isolated.provide("foo", 2)

    assert default_heard.calls == []
    assert isolated_heard.calls == [("foo", 2)]


def test_a_removed_provider_waits_for_its_dependents(root: Context) -> None:
    handle = root.provide("foo", 1)
    (impl,) = root.fiber.provisions
    dependent = object()
    impl.dependents.add(dependent)

    result = handle()

    assert result is not None
    assert not result.done()
    assert root.get("foo", strict=False) is None
    assert impl in root.fiber.provisions
    assert impl.release is not None
    impl.release()
    assert result.done()
    assert not root.fiber.provisions


def test_a_removed_provider_without_dependents_leaves_at_once(root: Context) -> None:
    result = root.provide("foo", 1)()

    assert result is not None
    assert result.done()
    assert not root.fiber.provisions


def test_an_accessor_computes_reads_and_writes(root: Context) -> None:
    store = {"value": 1}
    receivers: list[object] = []

    def get(ctx: Context, receiver: object) -> int:
        receivers.append(receiver)
        return store["value"]

    def set_(ctx: Context, value: int, receiver: object) -> None:
        receivers.append(receiver)
        store.update(value=value)

    handle = root.accessor("answer", get, set_)

    assert root["answer"] == 1
    root["answer"] = 5
    assert store == {"value": 5}
    # a plain access has no receiver
    assert receivers == [None, None]
    handle()
    assert root["answer"] is None


def test_an_accessor_without_a_setter_is_read_only(root: Context) -> None:
    root.accessor("answer", lambda ctx, receiver: 42)

    with pytest.raises(CordisError, match='property "answer" is read-only'):
        root["answer"] = 0


@pytest.mark.parametrize(
    ("mixins", "name"),
    [(["bar"], "bar"), ({"bar": "alias"}, "alias")],
)
def test_a_mixin_forwards_to_the_service_and_is_reverted(
    root: Context, mixins: Any, name: str
) -> None:
    service = SimpleNamespace(bar=1)
    root.provide("foo", service)
    handle = root.mixin("foo", mixins)

    assert root[name] == 1
    root[name] = 2
    assert service.bar == 2
    handle()
    assert root[name] is None


def test_a_mixin_gives_none_while_the_service_is_absent(root: Context) -> None:
    root.mixin("foo", ["bar"])

    assert root["bar"] is None


def test_a_mixin_is_one_effect_with_one_child_per_attribute(root: Context) -> None:
    root.mixin("foo", ["a", "b"])

    (effect,) = root.fiber.get_effects()
    assert effect.label == 'ctx.mixin("foo")'
    assert [child.label for child in effect.children] == [
        'ctx.accessor("a")',
        'ctx.accessor("b")',
    ]


@pytest.mark.contract("EFF-REVERT")
@pytest.mark.parametrize("seed", range(20))
def test_reverting_every_provision_restores_the_provider_tables(
    root: Context, seed: int
) -> None:
    rng = random.Random(seed)
    names = ["a", "b", "c"]
    handles: dict[str, Any] = {}
    for _ in range(30):
        name = rng.choice(names)
        if name in handles and rng.random() < 0.5:
            handles.pop(name)()
        elif name not in handles:
            handles[name] = root.provide(name, rng.random())
    root.mixin("a", ["x"])

    root.fiber.dispose()

    assert [root.get(name, strict=False) for name in names] == [None] * 3, seed
    assert not root.fiber.provisions, seed
    assert root.fiber.get_effects() == [], seed
    for name in names:
        root.provide(name)


def message(call: Callable[[], object]) -> str | None:
    """Return the message of the `CordisError` that `call` raises, or `None`."""
    try:
        call()
    except CordisError as error:
        return str(error)
    return None


@pytest.mark.upstream("reflect.spec.ts::Reflect::access check")
def test_a_component_context_reads_and_sets_only_declared_services(
    root: Context,
) -> None:
    results: list[object] = []

    def first(ctx: Context, config: object) -> None:
        results.append(message(lambda: ctx["bar"]))
        results.append(message(lambda: ctx.__setitem__("bar", 0)))

    def second(ctx: Context, config: object) -> None:
        results.append(message(lambda: ctx.__setitem__("foo", 0)))
        results.append(message(lambda: ctx.provide("foo")))
        results.append(message(lambda: ctx.provide("foo")))
        results.append(message(lambda: ctx.__setitem__("foo", 0)))

    root.use(first)
    root.use(second)

    assert results == [
        'cannot get property "bar" without inject',
        'cannot set property "bar" without provide',
        'cannot set property "foo" without provide',
        None,
        'service "foo" has been registered at <second>',
        None,
    ]


@pytest.mark.upstream("reflect.spec.ts::Reflect::service injection")
def test_a_derived_context_of_an_injecting_component_reads_its_meta(
    root: Context,
) -> None:
    seen: list[object] = []
    root.mixin("foo", ["bar"])
    root.provide("foo")
    root.set("foo", SimpleNamespace(bar=1))

    def child(ctx: Context, config: object) -> None:
        seen.append(ctx.meta["baz"])

    root.inject(["foo"], lambda ctx: ctx.extend(baz=2).use(child))

    assert root.get("foo") is not None
    assert root.get("bar") is None
    assert seen == [2]


@pytest.mark.upstream("reflect.spec.ts::Reflect::access error")
def test_an_access_error_names_the_rule_that_the_access_breaks(
    root: Context,
) -> None:
    root.provide("foo")
    root.set("foo", SimpleNamespace(bar=1))
    results: list[object] = []

    def inside(ctx: Context) -> None:
        results.append(message(lambda: ctx["bar"]))
        results.append(message(lambda: ctx.__setitem__("bar", 0)))

    fiber = root.inject(["foo"], inside)
    fiber.dispose()

    assert results == [
        'cannot get property "bar" without inject',
        'cannot set property "bar" without provide',
    ]
    assert message(lambda: fiber.ctx["foo"]) == (
        'cannot get required service "foo" in inactive context'
    )


@pytest.mark.upstream("reflect.spec.ts::Reflect::service inject leak")
def test_a_disposed_fiber_cannot_read_its_service(root: Context) -> None:
    root.provide("foo")
    root.set("foo", SimpleNamespace(bar=1))
    fiber = root.inject(["foo"], lambda ctx: None)

    assert fiber.ctx["foo"] is not None
    fiber.dispose()

    assert message(lambda: fiber.ctx["foo"]) == (
        'cannot get required service "foo" in inactive context'
    )


@pytest.mark.upstream(
    "reflect.spec.ts::Reflect::does not load a fiber disposed during service "
    "notification"
)
def test_a_fiber_disposed_during_notification_does_not_load(root: Context) -> None:
    apply = Recorder()
    component = SimpleNamespace(inject=["foo"], apply=apply)
    fiber1 = root.use(component, {"id": 1})
    fiber2 = root.use(component, {"id": 2})
    root.on(
        "internal/status",
        lambda fiber, old: (
            fiber2.dispose()
            if fiber is fiber1 and fiber.state == FiberState.LOADING
            else None
        ),
    )

    root.provide("foo", {})

    assert [call[1]["id"] for call in apply.calls] == [1]
    assert fiber2.state == FiberState.DISPOSED
    root.fiber.dispose()


def test_a_component_context_reads_a_service_through_the_get_listener(
    root: Context,
) -> None:
    root.provide("foo", 1)
    seen: list[object] = []

    def wrap(ctx: Context, name: str, next_: Callable[[], Any]) -> Any:
        return next_() + 10

    root.on("internal/get", wrap)

    def reader(ctx: Context) -> None:
        seen.append(ctx["foo"])

    root.inject(["foo"], reader)

    assert seen == [11]


def test_the_get_listener_sees_an_access_that_no_fiber_admits(root: Context) -> None:
    names: list[str] = []
    root.on("internal/get", lambda ctx, name, next_: names.append(name) or next_())
    results: list[object] = []

    root.use(lambda ctx, config: results.append(message(lambda: ctx["nope"])))

    assert names == ["nope"]
    assert results == ['cannot get property "nope" without inject']


HINT = (
    'declare "metrics" in inject, or use ctx.inject(["metrics"], callback) for an '
    "optional service"
)
READS: dict[str, Callable[[Context], object]] = {
    "item": lambda ctx: ctx["metrics"],
    "get": lambda ctx: ctx.get("metrics"),
}


@pytest.mark.parametrize(
    ("read", "declared", "notes"),
    [
        ("item", False, [HINT]),
        pytest.param(
            "get",
            False,
            [HINT],
            marks=[
                pytest.mark.divergence("checked-get"),
                pytest.mark.contract("LIFE-ORDER"),
            ],
        ),
        ("item", True, []),
    ],
)
def test_only_an_undeclared_access_names_both_declarations(
    root: Context, read: str, declared: bool, notes: list[str]
) -> None:
    # a sibling provides the service, so that no ancestor of the reader admits it
    root.use(lambda ctx, config: ctx.provide("metrics", 1))
    if declared:
        fiber = root.inject(["metrics"], lambda ctx: None)
        fiber.dispose()
        reader = fiber.ctx
    else:
        reader = root.use(lambda ctx, config: None).ctx

    with pytest.raises(CordisError) as caught:
        READS[read](reader)

    assert getattr(caught.value, "__notes__", []) == notes


def outcome(call: Callable[[], object]) -> object:
    """Return what `call` returns, or the message of the `CordisError` it raises."""
    try:
        return call()
    except CordisError as error:
        return str(error)


def reads(ctx: Context) -> tuple[object, ...]:
    """Read `foo` from `ctx` four ways.

    The ways are `ctx[key]`, `ctx.get(key)`, `ctx.get(key, strict=False)`, and the
    unchecked host lookup `ctx.reflect.get(ctx, key)`.
    """
    return (
        outcome(lambda: ctx["foo"]),
        outcome(lambda: ctx.get("foo")),
        outcome(lambda: ctx.get("foo", strict=False)),
        ctx.reflect.get(ctx, "foo"),
    )


def read_undeclared(root: Context) -> tuple[object, ...]:
    # a sibling provides `foo`: a provision of the root would admit the read
    root.use(lambda ctx, config: ctx.provide("foo", 1))
    return reads(root.use(lambda ctx, config: None).ctx)


def read_loaded(root: Context) -> tuple[object, ...]:
    root.provide("foo", 1)
    return reads(root.inject(["foo"], lambda ctx: None).ctx)


def read_provided(root: Context) -> tuple[object, ...]:
    seen: list[tuple[object, ...]] = []

    def provider(ctx: Context, config: object) -> None:
        ctx.provide("foo", 1)
        # the fiber is still loading, so `strict` hides its own provision
        seen.append(reads(ctx))

    root.use(provider)
    return seen[0]


def read_unloaded(root: Context) -> tuple[object, ...]:
    root.provide("foo", 1)
    fiber = root.inject(["foo"], lambda ctx: None)
    fiber.dispose()
    return reads(fiber.ctx)


def read_hidden(root: Context) -> tuple[object, ...]:
    seen: list[tuple[object, ...]] = []
    provider = root.use(lambda ctx, config: ctx.provide("foo", 1))
    # the dependent unloads first, and reads while the provider's fiber disposes
    root.inject(
        ["foo"], lambda ctx: ctx.effect(lambda: lambda: seen.append(reads(ctx)))
    )
    provider.dispose()
    return seen[0]


def read_changed(root: Context) -> tuple[object, ...]:
    remove = root.provide("foo", 1)
    fiber = root.inject(["foo"], lambda ctx: None)
    with root.scheduler.call():
        remove()
        root.provide("foo", 2)
        # the fiber has not reloaded yet: it still resolves `foo` to the first provider
        return reads(fiber.ctx)


def read_across_an_isolate(root: Context) -> tuple[object, ...]:
    root.provide("foo", 1)
    return reads(root.inject(["foo"], lambda ctx: None).ctx.isolate("foo"))


def read_root(root: Context) -> tuple[object, ...]:
    root.provide("foo", 1)
    return reads(root)


def read_isolated_root(root: Context) -> tuple[object, ...]:
    root.provide("foo", 1)
    return reads(root.isolate("foo"))


NO_INJECT = 'cannot get property "foo" without inject'
INACTIVE = 'cannot get required service "foo" in inactive context'


@pytest.mark.divergence("checked-get")
@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        (read_undeclared, (NO_INJECT, NO_INJECT, NO_INJECT, 1)),
        (read_loaded, (1, 1, 1, 1)),
        (read_provided, (1, None, 1, None)),
        (read_unloaded, (INACTIVE, None, None, 1)),
        (read_hidden, (1, None, 1, None)),
        pytest.param(
            read_changed, (1, 1, 1, 2), marks=pytest.mark.contract("LIFE-COHERENT")
        ),
        (read_across_an_isolate, (NO_INJECT, NO_INJECT, NO_INJECT, None)),
        (read_root, (1, 1, 1, 1)),
        (read_isolated_root, (None, None, None, None)),
    ],
    ids=lambda value: value.__name__.removeprefix("read_") if callable(value) else None,
)
def test_get_admits_a_read_as_an_item_access_does_and_returns_none_where_it_raises(
    root: Context,
    scenario: Callable[[Context], tuple[object, ...]],
    expected: tuple[object, ...],
) -> None:
    assert scenario(root) == expected


@pytest.mark.parametrize("realm", [None, object()], ids=["same-realm", "other-realm"])
def test_a_child_fiber_reads_an_optional_service_without_holding_its_parent(
    root: Context, realm: object | None
) -> None:
    seen: list[object] = []

    def apply(ctx: Context, config: object) -> None:
        scope = ctx if realm is None else ctx.isolate("metrics", realm)
        scope.inject(["metrics"], lambda inner: seen.append(inner.get("metrics")))

    fiber = root.use(apply)
    assert (fiber.state, seen) == (FiberState.ACTIVE, [])

    provider = root if realm is None else root.isolate("metrics", realm)
    provider.provide("metrics", 1)

    assert (fiber.state, seen) == (FiberState.ACTIVE, [1])


@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        ("root", None),
        ("isolated root", None),
        ("component", "component"),
        ("shadowed root", "component"),
    ],
)
def test_the_def_site_decides_which_reads_are_checked(
    root: Context, reader: str, expected: str | None
) -> None:
    component = root.inject(["foo"], lambda ctx: None).ctx
    readers = {
        "root": root,
        "isolated root": root.isolate("foo"),
        "component": component,
        "shadowed root": root.with_shadow(component),
    }

    site = root.reflect.def_site(readers[reader])

    assert site is (None if expected is None else readers[expected])


@pytest.mark.parametrize("declared", ["inject", "provide"])
def test_a_fiber_admits_what_it_injects_or_provides(
    root: Context, declared: str
) -> None:
    root.provide("foo", 1)
    contexts: list[Context] = []

    def component(ctx: Context, config: object) -> None:
        if declared == "provide":
            ctx.provide("bar", 2)
        contexts.append(ctx)

    fiber = root.use(SimpleNamespace(inject=["foo"], apply=component))
    (ctx,) = contexts
    name = "foo" if declared == "inject" else "bar"

    found = root.reflect.admit(ctx, name)

    assert found.impl is not None
    assert found.impl.value == (1 if declared == "inject" else 2)
    assert found.fiber is (fiber if declared == "inject" else None)


def test_the_set_listener_wraps_a_write_to_a_service(root: Context) -> None:
    seen: list[object] = []

    def wrap(ctx: Context, name: str, value: Any, next_: Callable[[], Any]) -> Any:
        seen.append((name, value))
        return next_()

    root.on("internal/set", wrap)

    def component(ctx: Context, config: object) -> None:
        ctx.provide("foo", 0)
        ctx["foo"] = 5

    root.use(component)

    assert seen == [("foo", 5)]
    assert root.get("foo") == 5


def test_a_fiber_cannot_set_a_service_of_another_fiber(root: Context) -> None:
    root.provide("foo", 0)
    results: list[object] = []

    root.inject(["foo"], lambda ctx: results.append(message(lambda: ctx.set("foo", 1))))

    assert results == ['cannot set property "foo" in multiple fibers']
    assert root.get("foo") == 0


def test_a_component_reads_a_service_through_its_parent_injection(
    root: Context,
) -> None:
    root.provide("foo", 1)
    results: list[object] = []

    def child(ctx: Context, config: object) -> None:
        results.append(message(lambda: ctx["foo"]) or ctx["foo"])

    def parent(ctx: Context) -> None:
        ctx.use(child)
        ctx.isolate("foo").use(child)

    root.inject(["foo"], parent)

    assert results == [1, 'cannot get property "foo" without inject']


def test_only_the_fibers_in_the_realm_of_a_provider_load(root: Context) -> None:
    apply = Recorder()
    isolated = root.isolate("foo")
    isolated.use(SimpleNamespace(inject=["foo"], apply=apply))
    root.provide("foo", 1)

    assert apply.calls == []

    isolated.provide("foo", 2)

    assert len(apply.calls) == 1
