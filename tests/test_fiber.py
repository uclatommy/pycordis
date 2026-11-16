"""Fiber lifecycle: inertia, failure, disposal states, and config validation on load."""

import logging
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock, Recorder, event

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import Issue, ValidationError
from pycordis.fiber import Fiber, FiberState
from pycordis.service import Service


def errors(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def record_status(ctx: Context, fiber: Fiber) -> list[tuple[FiberState, FiberState]]:
    """Return a list that gets each `(old, new)` state change of `fiber`."""
    transitions: list[tuple[FiberState, FiberState]] = []

    def listen(current: Fiber, old: FiberState) -> None:
        if current is fiber:
            transitions.append((old, current.state))

    ctx.on("internal/status", listen)
    return transitions


def slow_component(clock: FakeClock) -> Callable[[Context], Any]:
    """A component that needs 1000 ms to load and 1000 ms to unload."""

    async def apply(ctx: Context) -> Callable[[], Deferred[None]]:
        await clock.sleep(1000)
        return lambda: clock.sleep(1000)

    return apply


@pytest.mark.upstream("fiber.spec.ts::Fiber::inertia lock 1")
def test_a_provider_that_returns_during_unload_is_used_after_the_unload(
    root: Context, clock: FakeClock
) -> None:
    dispose = root.provide("foo", 1)
    fiber = root.inject(["foo"], slow_component(clock))
    clock.advance(400)
    assert fiber.state == FiberState.LOADING
    dispose()
    clock.advance(400)
    assert fiber.state == FiberState.LOADING
    clock.advance(400)
    assert fiber.state == FiberState.UNLOADING
    root.provide("foo", 1)
    clock.advance(1000)
    assert fiber.state == FiberState.LOADING
    clock.advance(1000)
    assert fiber.state == FiberState.ACTIVE


@pytest.mark.upstream("fiber.spec.ts::Fiber::inertia lock 2")
def test_a_new_binding_supersedes_the_running_load(
    root: Context, clock: FakeClock
) -> None:
    dispose = root.provide("foo", 1)
    fiber = root.inject(["foo"], slow_component(clock))
    clock.advance(400)
    assert fiber.state == FiberState.LOADING
    dispose()
    clock.advance(400)
    assert fiber.state == FiberState.LOADING
    root.provide("foo", 2)
    clock.advance(400)
    assert fiber.state == FiberState.UNLOADING
    clock.advance(1000)
    assert fiber.state == FiberState.LOADING
    clock.advance(1000)
    assert fiber.state == FiberState.ACTIVE
    assert fiber.ctx["foo"] == 2


@pytest.mark.upstream("fiber.spec.ts::Fiber::inertia lock 3")
def test_a_fiber_parks_at_pending_when_its_provider_is_disposed(
    root: Context, clock: FakeClock
) -> None:
    class Foo(Service):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "foo")

    provider = root.use(Foo)
    fiber = root.inject(["foo"], slow_component(clock))
    clock.advance(400)
    assert fiber.state == FiberState.LOADING
    clock.advance(600)
    assert fiber.state == FiberState.ACTIVE

    provider.dispose()
    clock.advance(1000)

    assert fiber.state == FiberState.PENDING


@pytest.mark.upstream("fiber.spec.ts::Fiber::component error")
def test_a_failed_component_leaves_its_sibling_and_its_listeners_alone(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    callback = Recorder()

    def apply(ctx: Context, config: dict[str, bool] | None) -> None:
        ctx.on(event, callback)
        if not (config and config.get("foo")):
            raise RuntimeError("component error")

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber1 = root.use(apply)
        fiber2 = root.use(apply, {"foo": True})

    assert (fiber1.state, fiber2.state) == (FiberState.FAILED, FiberState.ACTIVE)
    assert str(fiber1.error) == "component error"
    assert len(errors(caplog)) == 1
    root.emit(event)
    assert len(callback.calls) == 1


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber::failed fiber does not re-enter on dependency refresh"
)
def test_a_failed_fiber_does_not_load_again_when_its_provider_changes(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    apply = Recorder(lambda ctx: 1 / 0)
    dispose = root.provide("foo", 1)

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.inject(["foo"], apply)
        assert fiber.state == FiberState.FAILED
        dispose()
        root.provide("foo", 2)

    assert len(apply.calls) == 1
    assert fiber.state == FiberState.FAILED


@pytest.mark.upstream("fiber.spec.ts::Fiber::dispose error")
def test_an_error_in_a_disposer_is_logged_and_the_dispose_completes(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    def boom() -> None:
        raise RuntimeError("test")

    revert = Recorder(boom)
    fiber = root.use(lambda ctx, config: revert)
    assert len(revert.calls) == 0

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        result = fiber.dispose()

    assert result.result() is None
    assert len(revert.calls) == 1
    assert len(errors(caplog)) == 1


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber::settles a pending fiber to DISPOSED on dispose"
)
@pytest.mark.upstream(
    "fiber.spec.ts::Fiber::settles a failed fiber to DISPOSED on dispose"
)
@pytest.mark.upstream("fiber.spec.ts::Fiber::keeps the active dispose sequence stable")
@pytest.mark.parametrize(
    ("setup", "before", "expected"),
    [
        (
            lambda root: root.inject(["never-provided"], lambda ctx: None),
            FiberState.PENDING,
            [(FiberState.PENDING, FiberState.DISPOSED)],
        ),
        (
            lambda root: root.use(lambda ctx, config: 1 / 0),
            FiberState.FAILED,
            [(FiberState.FAILED, FiberState.DISPOSED)],
        ),
        (
            lambda root: root.use(lambda ctx, config: None),
            FiberState.ACTIVE,
            [
                (FiberState.ACTIVE, FiberState.UNLOADING),
                (FiberState.UNLOADING, FiberState.DISPOSED),
            ],
        ),
    ],
    ids=["pending", "failed", "active"],
)
def test_dispose_settles_a_fiber_to_disposed_in_one_sequence(
    root: Context,
    caplog: pytest.LogCaptureFixture,
    setup: Callable[[Context], Fiber],
    before: FiberState,
    expected: list[tuple[FiberState, FiberState]],
) -> None:
    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = setup(root)
        transitions = record_status(root, fiber)
        assert fiber.state == before

        fiber.dispose().result()

    assert fiber.uid is None
    assert fiber.state == FiberState.DISPOSED
    assert transitions == expected


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
def test_wait_rejects_with_the_error_that_failed_the_fiber(
    root: Context, caplog: pytest.LogCaptureFixture, is_async: bool
) -> None:
    def sync_apply(ctx: Context, config: object) -> None:
        raise RuntimeError("boom")

    async def async_apply(ctx: Context, config: object) -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.use(async_apply if is_async else sync_apply)
        waiting = fiber.wait()

    assert fiber.state == FiberState.FAILED
    with pytest.raises(RuntimeError, match="boom"):
        waiting.result()
    assert len(errors(caplog)) == 1


@pytest.mark.upstream("fiber.spec.ts::Fiber::restart wrapped fiber")
def test_restart_reverts_the_fiber_and_applies_it_again(root: Context) -> None:
    callback = Recorder()
    fiber = root.use(callback)

    fiber.restart().result()

    assert len(callback.calls) == 2
    assert fiber.state == FiberState.ACTIVE


@pytest.mark.upstream("fiber.spec.ts::Fiber: config validation::fail on first load")
def test_an_invalid_config_fails_the_fiber_before_apply_runs(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    apply = Recorder()

    def config(value: dict[str, Any]) -> dict[str, Any]:
        if not value["ok"]:
            raise ValueError("not ok")
        return {**value, "resolved": True}

    component = type("Component", (), {"apply": staticmethod(apply), "Config": config})

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.use(component, {"ok": False})

    assert len(apply.calls) == 0
    assert fiber.state == FiberState.FAILED
    assert isinstance(fiber.error, ValidationError)


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber: config validation::keep the issues and their paths"
)
def test_a_validation_error_keeps_its_issues_and_lists_their_paths() -> None:
    issues = [Issue("not a number", ("items", 0)), Issue("required", ())]

    error = ValidationError(issues)

    assert error.issues == tuple(issues)
    assert str(error) == "invalid config:\n  - not a number (at items.0)\n  - required"


def config_component(apply: Callable[..., Any]) -> Any:
    """A component whose `Config` validates `ok` and adds `resolved`."""

    def config(value: dict[str, Any]) -> dict[str, Any]:
        if not value["ok"]:
            raise ValueError("not ok")
        return {**value, "resolved": True}

    return SimpleNamespace(apply=apply, Config=config)


@pytest.mark.upstream("fiber.spec.ts::Fiber::update recovers a failed fiber")
def test_update_loads_a_failed_fiber_again(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    fixed = [False]

    def apply(ctx: Context) -> None:
        if not fixed[0]:
            raise RuntimeError("boom")

    root.provide("foo", 1)
    calls = Recorder(apply)
    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.inject(["foo"], calls)
    assert fiber.state == FiberState.FAILED

    fixed[0] = True
    fiber.update(None).result()

    assert len(calls.calls) == 2
    assert fiber.state == FiberState.ACTIVE


@pytest.mark.upstream("fiber.spec.ts::Fiber::update waits for asynchronous recovery")
def test_update_resolves_when_an_asynchronous_recovery_ends(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    gate = Deferred[None]()

    async def apply(ctx: Context, config: dict[str, bool]) -> None:
        if not config["fixed"]:
            raise RuntimeError("boom")
        await gate

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.use(apply, {"fixed": False})
    assert fiber.state == FiberState.FAILED

    updating = fiber.update({"fixed": True})
    assert not updating.done()
    gate.resolve(None)

    assert updating.done()
    assert fiber.state == FiberState.ACTIVE


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber::update surfaces a failed recovery to its caller"
)
def test_update_rejects_when_the_recovery_fails(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    def apply(ctx: Context, config: dict[str, str]) -> None:
        raise RuntimeError(config["message"])

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.use(apply, {"message": "initial failure"})
        updating = fiber.update({"message": "retry failure"})

    with pytest.raises(RuntimeError, match="retry failure"):
        updating.result()
    assert fiber.state == FiberState.FAILED


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber::update surfaces a failed reload to its caller"
)
@pytest.mark.upstream("fiber.spec.ts::Fiber::update does not leak a dropped failure")
@pytest.mark.parametrize("keep_result", [True, False], ids=["awaited", "dropped"])
def test_update_reports_a_failed_reload_through_the_fiber_and_its_result(
    root: Context, caplog: pytest.LogCaptureFixture, keep_result: bool
) -> None:
    fail = [False]

    def apply(ctx: Context, config: object) -> None:
        if fail[0]:
            raise RuntimeError("boom")

    fiber = root.use(apply)
    fail[0] = True
    with caplog.at_level(logging.ERROR, logger="pycordis"):
        updating = fiber.update({})

    assert fiber.state == FiberState.FAILED
    assert len(errors(caplog)) == 1
    if keep_result:
        with pytest.raises(RuntimeError, match="boom"):
            updating.result()


@pytest.mark.upstream("fiber.spec.ts::Fiber::update config on wrapped fiber")
def test_update_runs_the_component_with_each_new_config(root: Context) -> None:
    callback = Recorder()
    fiber = root.use(callback, {"msg": "hello"})
    assert [call[1] for call in callback.calls] == [{"msg": "hello"}]

    fiber.update({"msg": "world"})
    fiber.update({"msg": "!!!"})

    assert [call[1] for call in callback.calls] == [
        {"msg": "hello"},
        {"msg": "world"},
        {"msg": "!!!"},
    ]


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber::update config while injected service reloads"
)
def test_a_consumer_updated_while_its_provider_reloads_loads_once(
    root: Context, run: Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]
) -> None:
    applied: list[tuple[int, str]] = []

    class Provider(Service[Any]):
        def __init__(self, ctx: Context, config: dict[str, int]) -> None:
            super().__init__(ctx, "provider")
            self.value = config["value"]

    def consume(ctx: Context, config: dict[str, str]) -> None:
        applied.append((ctx["provider"].value, config["mode"]))

    provider = root.use(Provider, {"value": 1})
    consumer = root.use(
        SimpleNamespace(inject=["provider"], apply=consume), {"mode": "old"}
    )

    async def update_both() -> None:
        # both calls run before the scheduler takes another step
        updates = [provider.update({"value": 2}), consumer.update({"mode": "new"})]
        for update in updates:
            await update

    run(update_both()).result()

    assert applied == [(1, "old"), (2, "new")]
    assert consumer.config == {"mode": "new"}
    assert consumer.state == FiberState.ACTIVE


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber: config validation::keep a running component on update"
)
def test_an_invalid_config_on_update_keeps_the_running_component(root: Context) -> None:
    apply = Recorder()
    fiber = root.use(config_component(apply), {"ok": True})

    with pytest.raises(ValidationError, match="invalid config"):
        fiber.update({"ok": False})

    assert len(apply.calls) == 1
    assert fiber.state == FiberState.ACTIVE
    assert fiber.config == {"ok": True}


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber: config validation::pass the resolved config to listeners"
)
def test_update_listeners_receive_the_resolved_config(root: Context) -> None:
    received: list[Any] = []
    applied: list[Any] = []

    def apply(ctx: Context, config: Any) -> None:
        applied.append(config)

        def listen(fiber: Fiber, config: Any, next_: Callable[[], Any]) -> Any:
            received.append(config)
            return next_()

        ctx.on("internal/update", listen)

    fiber = root.use(config_component(apply), {"ok": True})
    assert applied == [{"ok": True, "resolved": True}]

    fiber.update({"ok": True, "extra": 1}).result()

    assert received == [{"ok": True, "extra": 1, "resolved": True}]
    assert fiber.config == {"ok": True, "extra": 1}


@pytest.mark.upstream(
    "fiber.spec.ts::Fiber: config validation::keep the source config when a listener "
    "handles the update"
)
@pytest.mark.divergence("update-result")
def test_a_listener_that_handles_an_update_keeps_the_source_config(
    root: Context,
) -> None:
    received: list[Any] = []
    applied: list[Any] = []

    def apply(ctx: Context, config: Any) -> None:
        applied.append(config)
        ctx.on("internal/update", lambda fiber, config, next_: received.append(config))

    fiber = root.use(config_component(apply), {"ok": True})
    source = {"ok": True, "extra": 1}

    updating = fiber.update(source)

    assert updating.done()
    updating.result()
    assert received == [{"ok": True, "extra": 1, "resolved": True}]
    assert len(applied) == 1
    assert fiber.config is source

    fiber.restart().result()
    assert applied == [
        {"ok": True, "resolved": True},
        {"ok": True, "extra": 1, "resolved": True},
    ]


@pytest.mark.divergence("host-introspection")
def test_a_host_reads_the_fiber_tree_the_failure_and_the_effect_labels(
    root: Context,
) -> None:
    def parent(ctx: Context, config: object) -> None:
        ctx.on("custom", lambda: None)
        ctx.provide("service", 1)
        ctx.use(child)

    def child(ctx: Context, config: dict[str, bool]) -> None:
        if config["fail"]:
            raise ValueError("child failed")

    outer = root.use(parent)
    first = outer.ctx.use(child, {"fail": False})
    failing = outer.ctx.use(child, {"fail": True})

    assert [fiber.parent.fiber for fiber in outer.children] == [outer] * 3
    assert outer.children[1:] == (first, failing)
    assert root.fiber.children == (outer,)
    assert [meta.label for meta in outer.get_effects()][:2] == [
        'ctx.on("custom")',
        'ctx.provide("service")',
    ]
    assert failing.state == FiberState.FAILED
    assert isinstance(failing.error, ValueError)
    assert first.error is None

    failing.update({"fail": False})

    assert failing.state == FiberState.ACTIVE
    assert failing.error is None

    first.dispose()

    assert first not in outer.children
    assert len(outer.children) == 2
