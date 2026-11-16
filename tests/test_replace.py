"""`RegistryService.replace` moves live fibers to a new component."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.fiber import Fiber, FiberState


class Recorder:
    """A component function that records the config of each call."""

    def __init__(self, on_dispose: Callable[[], None] | None = None) -> None:
        self.configs: list[Any] = []
        self._on_dispose = on_dispose

    def __call__(self, ctx: Context, config: Any) -> Callable[[], None] | None:
        self.configs.append(config)
        return self._on_dispose


def noop(ctx: Context, config: Any) -> None:
    return None


def fail(ctx: Context, config: Any) -> None:
    raise RuntimeError("failed")


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::swaps the implementation behind a fiber "
    "in place"
)
def test_a_replaced_fiber_keeps_its_identity_and_runs_the_new_component(
    root: Context,
) -> None:
    disposed: list[str] = []
    old = Recorder(lambda: disposed.append("old"))
    new = Recorder()
    fiber = root.use(old, {"foo": 1})
    uid = fiber.uid

    replaced = root.registry.replace(old, new)

    assert replaced == [fiber]
    assert disposed == ["old"]
    assert new.configs == [{"foo": 1}]
    assert (fiber.uid, fiber.state) == (uid, FiberState.ACTIVE)
    assert fiber.runtime is root.registry.get(new)
    assert list(root.registry.keys()) == [new]


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::emits internal/replace for each moved fiber"
)
def test_internal_replace_fires_for_each_fiber_with_its_new_runtime(
    root: Context,
) -> None:
    runtimes: list[Any] = []
    root.on("internal/replace", lambda fiber: runtimes.append(fiber.runtime))
    first, second = root.use(noop), root.use(noop)
    new = Recorder()

    replaced = root.registry.replace(noop, new)

    assert replaced == [first, second]
    assert runtimes == [root.registry.get(new)] * 2


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::validates the config with the new schema"
)
def test_the_new_config_schema_validates_the_stored_config(root: Context) -> None:
    new = Recorder()
    old = SimpleNamespace(Config=lambda value: {**value, "version": 1}, apply=noop)
    root.use(old, {"foo": 1})

    root.registry.replace(
        old,
        SimpleNamespace(Config=lambda value: {**value, "version": 2}, apply=new),
    )

    assert new.configs == [{"foo": 1, "version": 2}]


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::takes the dependencies of the new component"
)
def test_a_fiber_takes_the_inject_of_the_new_component(root: Context) -> None:
    new = Recorder()
    old = SimpleNamespace(apply=noop)
    fiber = root.use(old)

    root.registry.replace(old, SimpleNamespace(inject=["dep"], apply=new))
    assert (list(fiber.inject), fiber.state, new.configs) == (
        ["dep"],
        FiberState.PENDING,
        [],
    )

    root.provide("dep", {})
    assert (fiber.state, len(new.configs)) == (FiberState.ACTIVE, 1)


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::starts a pending fiber when the new "
    "component "
    "drops the missing dependency"
)
@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::starts a pending fiber when the new "
    "component "
    "injects an available service"
)
@pytest.mark.parametrize("new_inject", [[], ["available"]])
def test_a_pending_fiber_starts_when_the_new_inject_is_satisfied(
    root: Context, new_inject: list[str]
) -> None:
    root.provide("available", {})
    old = SimpleNamespace(inject=["missing"], apply=noop)
    new = Recorder()
    fiber = root.use(old)
    assert fiber.state == FiberState.PENDING

    root.registry.replace(old, SimpleNamespace(inject=new_inject, apply=new))

    assert (fiber.state, len(new.configs)) == (FiberState.ACTIVE, 1)


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::stops depending on a dependency that the new "
    "component drops"
)
def test_a_provider_does_not_wait_for_a_fiber_that_dropped_its_dependency(
    root: Context,
) -> None:
    provider = root.use(lambda ctx, config: ctx.provide("dep", {}))
    old = SimpleNamespace(inject=["dep"], apply=noop)
    new = Recorder()
    fiber = root.use(old)

    root.registry.replace(old, new)
    restarted = provider.restart()

    assert restarted.done()
    assert (fiber.state, len(new.configs)) == (FiberState.ACTIVE, 1)


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::reloads a consumer of a replaced service"
)
def test_a_consumer_reloads_against_the_service_of_the_new_component(
    root: Context,
) -> None:
    values: list[str] = []

    def old(ctx: Context, config: Any) -> None:
        ctx.provide("replaced", {"value": "old"})

    def new(ctx: Context, config: Any) -> None:
        ctx.provide("replaced", {"value": "new"})

    root.use(old)
    consumer = root.use(
        SimpleNamespace(
            inject=["replaced"],
            apply=lambda ctx, config: values.append(ctx["replaced"]["value"]),
        )
    )

    root.registry.replace(old, new)

    assert values == ["old", "new"]
    assert consumer.state == FiberState.ACTIVE


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::merges the fibers into a registered component"
)
def test_fibers_join_the_runtime_of_a_registered_component(root: Context) -> None:
    old, new = Recorder(), Recorder()
    first = root.use(old, "a")
    second = root.use(new, "b")

    root.registry.replace(old, new)

    assert len(root.registry) == 1
    assert first.runtime is second.runtime
    assert sorted(fiber.config for fiber in first.runtime.fibers) == ["a", "b"]
    # the fiber of `new` does not restart
    assert new.configs == ["b", "a"]


@pytest.mark.upstream("replace.spec.ts::registry.replace()::restarts a failed fiber")
def test_a_failed_fiber_loads_the_new_component(root: Context) -> None:
    new = Recorder()
    fiber = root.use(fail)
    assert fiber.state == FiberState.FAILED

    root.registry.replace(fail, new)

    assert (fiber.state, len(new.configs)) == (FiberState.ACTIVE, 1)
    assert fiber.error is None


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::leaves a fiber failed when the new component "
    "fails to apply"
)
def test_a_new_component_that_raises_leaves_the_fiber_failed(root: Context) -> None:
    fiber = root.use(noop)

    root.registry.replace(noop, fail)

    assert fiber.state == FiberState.FAILED
    with pytest.raises(RuntimeError, match="failed"):
        fiber.wait().result()


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::forwards the old component to the new one"
)
def test_use_and_get_follow_a_replaced_component(root: Context) -> None:
    old, apply = Recorder(), Recorder()
    new = SimpleNamespace(inject=["dep"], apply=apply)
    root.use(old)
    root.registry.replace(old, new)

    fiber = root.use(old)

    assert list(fiber.inject) == ["dep"]
    assert fiber.runtime is root.registry.get(new)
    assert root.registry.get(old) is root.registry.get(new)
    # the alias is not a registered component
    assert list(root.registry.keys()) == [apply]
    root.provide("dep", {})
    assert (len(old.configs), len(apply.configs)) == (1, 2)


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::forwards a component that is not registered"
)
def test_a_component_without_fibers_is_forwarded(root: Context) -> None:
    old, new = Recorder(), Recorder()

    assert root.registry.replace(old, new) == []
    assert len(root.registry) == 0

    root.use(old)
    assert (len(old.configs), len(new.configs)) == (0, 1)


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::follows a chain of replacements"
)
def test_a_chain_of_replacements_ends_at_the_last_component(root: Context) -> None:
    a, b, c = Recorder(), Recorder(), Recorder()
    fiber = root.use(a)

    root.registry.replace(a, b)
    root.registry.replace(a, c)

    assert fiber.runtime is root.registry.get(c)
    assert list(root.registry.keys()) == [c]


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::switches back to a replaced component"
)
def test_a_replaced_component_can_replace_its_replacement(root: Context) -> None:
    a, b = Recorder(), Recorder()
    fiber = root.use(a)

    root.registry.replace(a, b)
    root.registry.replace(b, a)

    assert fiber.runtime is root.registry.get(a)
    assert list(root.registry.keys()) == [a]
    assert len(a.configs) == 2


@pytest.mark.upstream(
    "replace.spec.ts::registry.replace()::unregisters the new component when its last "
    "fiber is disposed"
)
def test_the_new_component_unregisters_with_its_last_fiber(root: Context) -> None:
    fiber: Fiber = root.use(noop)
    new = Recorder()
    root.registry.replace(noop, new)

    fiber.dispose()

    assert len(root.registry) == 0
    assert not root.registry.has(new)


@pytest.mark.parametrize("invalid", ["old", "new"])
def test_replace_rejects_a_value_that_is_not_a_component(
    root: Context, invalid: str
) -> None:
    arguments = (noop, 1) if invalid == "new" else (1, noop)
    with pytest.raises(TypeError, match="invalid component"):
        root.registry.replace(*arguments)


def test_replacing_a_component_with_itself_runs_its_fibers_again(root: Context) -> None:
    disposed: list[str] = []
    component = Recorder(lambda: disposed.append("reverted"))
    fiber = root.use(component, 1)

    assert root.registry.replace(component, component) == [fiber]

    assert (disposed, component.configs) == (["reverted"], [1, 1])
    assert (fiber.state, list(root.registry.keys())) == (
        FiberState.ACTIVE,
        [component],
    )


def test_a_disposed_fiber_does_not_move(root: Context) -> None:
    kept = root.use(noop)
    root.use(noop).dispose()
    new = Recorder()

    assert root.registry.replace(noop, new) == [kept]
    assert len(new.configs) == 1
