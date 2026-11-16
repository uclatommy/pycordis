"""Components: the three forms, names, config validation, and loading by injection."""

import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import Recorder, event, hook_snapshot

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError, Issue, ValidationError
from pycordis.fiber import EffectMeta, FiberState
from pycordis.keys import Key


def capture(call: Callable[[], object]) -> str | None:
    """Return the message of the `CordisError` that `call` raises, or `None`."""
    try:
        call()
    except CordisError as error:
        return str(error)
    return None


@pytest.mark.upstream("component.spec.ts::Component::apply functional component")
def test_a_function_component_receives_the_context_and_the_config(
    root: Context,
) -> None:
    callback = Recorder()
    options = {"foo": "bar"}

    root.use(callback, options)

    assert len(callback.calls) == 1
    assert callback.calls[0][1] == options


@pytest.mark.upstream("component.spec.ts::Component::apply object component")
def test_an_object_component_runs_its_apply_method(root: Context) -> None:
    callback = Recorder()
    options = {"bar": "foo"}

    root.use(SimpleNamespace(apply=callback), options)

    assert len(callback.calls) == 1
    assert callback.calls[0][1] == options


@pytest.mark.upstream("component.spec.ts::Component::Service.init")
def test_a_class_component_runs_its_init_hook_and_reverts_the_result(
    root: Context,
) -> None:
    start, stop = Recorder(), Recorder()

    class Foo:
        def __init__(self, ctx: Context, config: object) -> None:
            self.ctx = ctx

        def cordis_init(self) -> Callable[[], None]:
            start()
            return stop

    fiber = root.use(Foo)
    assert (len(start.calls), len(stop.calls)) == (1, 0)

    fiber.dispose()

    assert (len(start.calls), len(stop.calls)) == (1, 1)


@pytest.mark.upstream("component.spec.ts::Component::apply invalid component")
@pytest.mark.parametrize(
    "component", [None, {}, SimpleNamespace(apply={}), SimpleNamespace()]
)
def test_an_invalid_component_is_rejected(root: Context, component: Any) -> None:
    with pytest.raises(
        TypeError, match='invalid component, expect function or object with an "apply"'
    ):
        root.use(component)

    assert len(root.registry) == 0


@pytest.mark.upstream("component.spec.ts::Component::inactive context")
def test_an_inactive_context_cannot_register_anything(root: Context) -> None:
    callback = Recorder()
    outcomes: list[str | None] = []

    def component(ctx: Context, config: object) -> Callable[[], None]:
        def dispose() -> None:
            outcomes.append(capture(lambda: ctx.use(callback)))
            outcomes.append(capture(lambda: ctx.effect(lambda: lambda: None)))
            outcomes.append(capture(lambda: ctx.on("custom-event", lambda: None)))

        return dispose

    root.use(component).dispose()

    assert outcomes == ["cannot create effect on inactive context"] * 3
    assert callback.calls == []


@pytest.mark.upstream("component.spec.ts::Component::context inspect")
def test_a_context_names_the_fiber_of_its_component(root: Context) -> None:
    names: list[str] = []

    def foo(ctx: Context, config: object) -> None:
        names.append(repr(ctx))
        # a component without a name takes the name of its parent
        ctx.use(lambda child, config: names.append(repr(child)))

    class Qux:
        def __init__(self, ctx: Context, config: object) -> None:
            names.append(repr(ctx))

    names.append(repr(root))
    root.use(lambda ctx, config: names.append(repr(ctx)))
    root.use(foo)
    root.use(
        SimpleNamespace(name="bar", apply=lambda ctx, config: names.append(repr(ctx)))
    )
    root.use(Qux)

    assert names == [
        "Context <root>",
        "Context <root>",
        "Context <foo>",
        "Context <foo>",
        "Context <bar>",
        "Context <Qux>",
    ]


@pytest.mark.parametrize(
    ("function_name", "name_attribute", "expected"),
    [
        ("apply", None, "root"),
        ("<lambda>", None, "root"),
        ("foo", None, "foo"),
        ("foo", "custom", "custom"),
    ],
)
def test_a_component_name_comes_from_its_name_attribute_or_function_name(
    root: Context, function_name: str, name_attribute: str | None, expected: str
) -> None:
    def component(ctx: Context, config: object) -> None:
        pass

    component.__name__ = function_name
    if name_attribute is not None:
        component.name = name_attribute  # type: ignore[attr-defined]

    assert root.use(component).name == expected


@pytest.mark.upstream("component.spec.ts::Component::ctx.registry")
def test_the_registry_lists_the_loaded_components(root: Context) -> None:
    def component(ctx: Context, config: object) -> None:
        pass

    assert (len(root.registry), root.registry.has(component)) == (0, False)
    fiber = root.use(component)

    runtime = root.registry.get(component)
    assert runtime is not None and list(runtime.fibers) == [fiber]
    assert list(root.registry.keys()) == [component]
    assert list(root.registry.values()) == [runtime]
    assert list(root.registry.entries()) == [(component, runtime)]
    seen: list[object] = []
    root.registry.for_each(lambda value, key: seen.append((value, key)))
    assert seen == [(runtime, component)]
    fiber.dispose()
    assert (len(root.registry), root.registry.get(component)) == (0, None)


@pytest.mark.upstream("component.spec.ts::Component::nested components")
def test_nested_components_end_with_their_parent(root: Context) -> None:
    async def component(ctx: Context, config: object) -> None:
        ctx.on(event, callback)
        await ctx.use(inner)

    async def inner(ctx: Context, config: object) -> None:
        ctx.on(event, callback)
        await ctx.use(innermost)

    def innermost(ctx: Context, config: object) -> None:
        ctx.on(event, callback)

    callback = Recorder()
    root.on(event, callback)
    fiber = root.use(component)

    assert callback.calls == []
    assert len(root.registry) == 3
    root.emit(event)
    assert len(callback.calls) == 4

    callback.calls.clear()
    fiber.dispose()
    assert len(root.registry) == 0
    root.emit(event)
    assert len(callback.calls) == 1

    # a second dispose does nothing
    callback.calls.clear()
    fiber.dispose()
    assert len(root.registry) == 0
    root.emit(event)
    assert len(callback.calls) == 1


@pytest.mark.upstream("component.spec.ts::Component::compare snapshot")
def test_disposing_a_component_restores_the_hook_tables(root: Context) -> None:
    async def component(ctx: Context, config: object) -> None:
        ctx.on(event, lambda: None)
        await ctx.use(inner)

    async def inner(ctx: Context, config: object) -> None:
        ctx.on(event, lambda: None)
        await ctx.use(lambda ctx, config: ctx.on(event, lambda: None) and None)

    before = hook_snapshot(root)
    fiber = root.use(component)
    after = hook_snapshot(root)
    fiber.dispose()
    assert hook_snapshot(root) == before

    root.use(component)
    assert hook_snapshot(root) == after


@pytest.mark.upstream("component.spec.ts::Component::root dispose")
def test_root_dispose_ends_every_component_and_keeps_the_root(root: Context) -> None:
    dispose = Recorder()
    fiber = root.use(lambda ctx, config: dispose)
    assert (root.fiber.uid, fiber.uid) == (0, 1)
    assert dispose.calls == [] and len(root.fiber.get_effects()) == 1

    root.fiber.dispose()

    assert (root.fiber.uid, fiber.uid) == (0, None)
    assert len(dispose.calls) == 1 and root.fiber.get_effects() == []
    assert fiber.state == FiberState.DISPOSED

    root.fiber.dispose()

    assert (root.fiber.uid, fiber.uid) == (0, None)
    assert len(dispose.calls) == 1 and root.fiber.get_effects() == []


@pytest.mark.upstream("dispose.spec.ts::Effects::dispose by component")
def test_disposing_a_fiber_runs_the_inverses_of_its_effects_once(root: Context) -> None:
    dispose = Recorder()

    fiber = root.use(lambda ctx, config: ctx.effect(lambda: dispose, "test") and None)

    assert fiber.get_effects() == [EffectMeta("test", [])]
    assert dispose.calls == []
    fiber.dispose()
    assert len(dispose.calls) == 1
    fiber.dispose()
    assert len(dispose.calls) == 1


def test_a_component_loads_when_its_service_has_a_provider(root: Context) -> None:
    log: list[object] = []

    def consumer(ctx: Context, config: object) -> Callable[[], None]:
        log.append(("load", ctx["foo"]))
        return lambda: log.append("unload")

    def provider(ctx: Context, config: object) -> None:
        ctx.provide("foo", 1)

    fiber = root.use(SimpleNamespace(inject=["foo"], apply=consumer))
    assert (fiber.state, log) == (FiberState.PENDING, [])

    supplier = root.use(provider)
    assert (fiber.state, log) == (FiberState.ACTIVE, [("load", 1)])

    supplier.dispose()
    assert (fiber.state, log) == (FiberState.PENDING, [("load", 1), "unload"])


_INTERCEPT = {"flag": True}


@pytest.mark.parametrize(
    ("inject", "expected"),
    [
        (["foo"], {"foo": None}),
        (("foo",), {"foo": None}),
        ([Key[int]("foo")], {"foo": None}),
        ({"foo": None}, {"foo": None}),
        ({Key[int]("foo"): _INTERCEPT}, {"foo": _INTERCEPT}),
        (["b", "a"], {"b": None, "a": None}),
        (None, {}),
    ],
)
def test_inject_resolves_to_names_in_order_with_intercept_configs(
    root: Context, inject: Any, expected: dict[str, object]
) -> None:
    fiber = root.use(SimpleNamespace(inject=inject, apply=lambda ctx, config: None))

    assert fiber.inject == expected
    assert list(fiber.inject) == list(expected)


@pytest.mark.parametrize("inject", ["foo", 3])
def test_inject_that_is_not_a_list_or_a_dict_is_a_type_error(
    root: Context, inject: Any
) -> None:
    with pytest.raises(TypeError, match="inject must be a list of names or a dict"):
        root.use(SimpleNamespace(inject=inject, apply=Recorder()))


def test_a_class_component_declares_inject_as_a_class_attribute(
    root: Context,
) -> None:
    loaded = Recorder()

    class Consumer:
        inject = ["foo"]

        def __init__(self, ctx: Context, config: object) -> None:
            loaded(ctx["foo"])

    root.use(Consumer)
    assert loaded.calls == []
    root.provide("foo", 7)

    assert loaded.calls == [(7,)]


def test_ctx_inject_runs_a_callback_when_the_services_exist(root: Context) -> None:
    seen = Recorder()

    def reader(ctx: Context) -> None:
        seen(ctx["foo"])

    fiber = root.inject(["foo"], reader)
    assert seen.calls == [] and fiber.name == "reader"

    root.provide("foo", 3)

    assert seen.calls == [(3,)]


def test_a_fiber_that_is_disposed_while_pending_does_not_load(root: Context) -> None:
    apply = Recorder()
    fiber = root.use(SimpleNamespace(inject=["foo"], apply=apply))

    fiber.dispose()
    root.provide("foo", 1)

    assert apply.calls == [] and fiber.state == FiberState.DISPOSED


class _FakeSchema:
    """A validator whose `model_validate` raises an error that has `errors()`."""

    class Failure(Exception):
        def errors(self) -> list[dict[str, Any]]:
            return [
                {"msg": "expected int", "loc": ("server", "port")},
                {"msg": "required", "loc": ()},
            ]

    @classmethod
    def model_validate(cls, config: dict[str, Any]) -> SimpleNamespace:
        if not isinstance(config.get("port"), int):
            raise cls.Failure
        return SimpleNamespace(**config)


def test_a_config_object_validates_the_config_before_apply(root: Context) -> None:
    apply = Recorder()

    fiber = root.use(SimpleNamespace(Config=_FakeSchema, apply=apply), {"port": 80})

    assert fiber.state == FiberState.ACTIVE
    assert apply.calls[0][1] == SimpleNamespace(port=80)


def test_a_config_function_transforms_the_config(root: Context) -> None:
    apply = Recorder()
    component = SimpleNamespace(Config=lambda raw: raw * 2, apply=apply)

    root.use(component, 21)

    assert apply.calls[0][1] == 42


def test_an_invalid_config_fails_the_fiber_with_a_validation_error(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    apply = Recorder()

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.use(SimpleNamespace(Config=_FakeSchema, apply=apply), {})

    assert apply.calls == [] and fiber.state == FiberState.FAILED
    assert isinstance(fiber.error, ValidationError)
    assert str(fiber.error) == (
        "invalid config:\n  - expected int (at server.port)\n  - required"
    )
    assert [record.message for record in caplog.records] == ["error while loading"]


def test_a_config_function_that_raises_is_a_validation_error(root: Context) -> None:
    def transform(raw: object) -> object:
        raise ValueError("bad value")

    fiber = root.use(SimpleNamespace(Config=transform, apply=Recorder()))

    assert isinstance(fiber.error, ValidationError)
    assert fiber.error.issues == (Issue("bad value"),)
    assert isinstance(fiber.error.__cause__, ValueError)


def test_a_config_that_is_not_a_validator_is_a_type_error(root: Context) -> None:
    fiber = root.use(SimpleNamespace(Config=3, apply=Recorder()))

    assert isinstance(fiber.error, TypeError)
    assert not isinstance(fiber.error, ValidationError)


async def _coroutine(value: object) -> object:
    return value


@pytest.mark.parametrize(
    "asynchronous",
    [Deferred.resolved, _coroutine],
    ids=["deferred", "coroutine"],
)
def test_a_config_validator_that_returns_an_awaitable_is_a_type_error(
    root: Context, asynchronous: Callable[[object], object]
) -> None:
    apply = Recorder()

    fiber = root.use(SimpleNamespace(Config=asynchronous, apply=apply), 1)

    assert apply.calls == [] and fiber.state == FiberState.FAILED
    assert isinstance(fiber.error, TypeError)
    assert str(fiber.error) == "Async config validation is not supported"


def test_the_config_waterfall_lets_a_listener_change_the_config(root: Context) -> None:
    apply = Recorder()
    root.on("internal/config", lambda fiber, next_: next_() + 1)

    root.use(apply, 1)

    assert apply.calls[0][1] == 2


def test_a_validation_error_lists_each_issue_with_its_path() -> None:
    error = ValidationError([Issue("a", ("x", 0, "y")), Issue("b")])

    assert str(error) == "invalid config:\n  - a (at x.0.y)\n  - b"
    assert isinstance(error, TypeError)


def test_the_error_of_a_failed_apply_names_the_use_call_site(root: Context) -> None:
    def component(ctx: Context, config: object) -> None:
        raise RuntimeError("boom")

    fiber = root.use(component)

    assert isinstance(fiber.error, RuntimeError)
    assert any("Effect created at" in note for note in fiber.error.__notes__)
    assert any("test_component.py" in note for note in fiber.error.__notes__)


def test_a_property_that_raises_makes_a_component_invalid(root: Context) -> None:
    class Broken:
        @property
        def apply(self) -> None:
            raise RuntimeError("no apply")

    with pytest.raises(TypeError, match="invalid component"):
        root.use(Broken())


def test_fibers_of_one_component_share_a_runtime_until_the_last_ends(
    root: Context,
) -> None:
    def component(ctx: Context, config: object) -> None:
        pass

    first, second = root.use(component), root.use(component)

    runtime = root.registry.get(component)
    assert runtime is not None and list(runtime.fibers) == [first, second]
    assert len(root.registry) == 1
    first.dispose()
    assert list(runtime.fibers) == [second] and len(root.registry) == 1
    second.dispose()
    assert len(root.registry) == 0


def test_a_fiber_reports_its_creation_disposal_and_state_changes(
    root: Context,
) -> None:
    log: list[object] = []
    root.on("internal/plugin", lambda fiber: log.append(("plugin", fiber.uid)))
    root.on(
        "internal/status",
        lambda fiber, old: log.append(("status", old, fiber.state)),
    )

    root.use(lambda ctx, config: None).dispose()

    assert log == [
        ("plugin", 1),
        ("status", FiberState.PENDING, FiberState.LOADING),
        ("status", FiberState.LOADING, FiberState.ACTIVE),
        ("plugin", None),
        ("status", FiberState.ACTIVE, FiberState.UNLOADING),
        ("status", FiberState.UNLOADING, FiberState.DISPOSED),
    ]


def test_a_validation_error_from_a_config_function_is_not_wrapped(
    root: Context,
) -> None:
    error = ValidationError([Issue("own", ("a",))])

    def transform(raw: object) -> object:
        raise error

    fiber = root.use(SimpleNamespace(Config=transform, apply=Recorder()))

    assert fiber.error is error
