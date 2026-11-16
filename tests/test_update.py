"""Concurrent updates of one fiber settle on the latest config."""

import logging
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.events import EventOptions
from pycordis.fiber import Fiber, FiberState


def done_count(*results: Deferred[Any]) -> int:
    return sum(result.done() for result in results)


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::settles concurrent updates on the "
    "latest config (inject: false)"
)
@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::settles concurrent updates on the "
    "latest config (inject: true)"
)
@pytest.mark.parametrize("inject", [False, True])
def test_updates_during_a_load_end_in_one_load_with_the_latest_config(
    root: Context, inject: bool
) -> None:
    root.provide("foo", 1)
    started, release = Deferred[None](), Deferred[None]()
    latest_started, latest_release = Deferred[None](), Deferred[None]()
    applied: list[int] = []
    disposed: list[int] = []

    async def apply(ctx: Context, config: dict[str, int]) -> Callable[[], None]:
        value = config["value"]
        applied.append(value)
        if inject:
            assert ctx["foo"] == 1
        if value == 1:
            started.resolve(None)
            await release
        elif value == 3:
            latest_started.resolve(None)
            await latest_release
        return lambda: disposed.append(value)

    fiber = root.use(
        SimpleNamespace(inject=["foo"] if inject else [], apply=apply), {"value": 0}
    )
    first = fiber.update({"value": 1})
    assert started.done()
    second = fiber.update({"value": 2})
    third = fiber.update({"value": 3})
    release.resolve(None)

    assert latest_started.done()
    assert applied == [0, 1, 3]
    assert disposed == [0, 1]
    assert done_count(first, second, third) == 0
    assert fiber.state == FiberState.LOADING
    latest_release.resolve(None)

    assert done_count(first, second, third) == 3
    assert fiber.config == {"value": 3}
    assert fiber.state == FiberState.ACTIVE
    root.fiber.dispose().result()
    assert disposed == [0, 1, 3]


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::skips superseded loads before "
    "executing the component"
)
def test_an_update_before_the_first_apply_skips_the_superseded_load(
    root: Context, run: Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]
) -> None:
    applied: list[int] = []

    async def body() -> None:
        fiber = root.use(
            lambda ctx, config: applied.append(config["value"]), {"value": 1}
        )
        first = fiber.update({"value": 2})
        second = fiber.update({"value": 3})
        await first
        await second
        await fiber

    run(body()).result()

    assert applied == [3]
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::coalesces updates during unloading"
)
def test_updates_during_an_unload_end_in_one_load(root: Context) -> None:
    unloading, release = Deferred[None](), Deferred[None]()
    applied: list[int] = []

    def apply(ctx: Context, config: dict[str, int]) -> Callable[[], Any]:
        value = config["value"]
        applied.append(value)

        def revert() -> Deferred[None] | None:
            if value != 0:
                return None
            unloading.resolve(None)
            return release

        return revert

    fiber = root.use(apply, {"value": 0})
    first = fiber.update({"value": 1})
    assert unloading.done()
    second = fiber.update({"value": 2})

    assert applied == [0]
    assert fiber.state == FiberState.UNLOADING
    release.resolve(None)

    assert first.done() and second.done()
    assert applied == [0, 2]
    assert fiber.state == FiberState.ACTIVE
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::honors restart while loading even "
    "when config is unchanged"
)
@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::honors config ABA while loading even "
    "when config is unchanged"
)
@pytest.mark.parametrize("mode", ["restart", "config ABA"])
def test_a_restart_during_a_load_runs_the_component_again(
    root: Context, mode: str
) -> None:
    release = Deferred[None]()
    original = {"value": 1}
    applied: list[int] = []

    async def apply(ctx: Context, config: dict[str, int]) -> None:
        applied.append(config["value"])
        await release

    fiber = root.use(apply, original)
    updates = (
        [fiber.restart()]
        if mode == "restart"
        else [fiber.update({"value": 2}), fiber.update(original)]
    )
    release.resolve(None)

    assert all(update.done() for update in updates)
    assert applied == [1, 1]
    assert fiber.config is original
    assert fiber.state == FiberState.ACTIVE
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::cleans up a superseded failure "
    "without failing the latest update"
)
def test_a_superseded_load_that_fails_is_cleaned_up_and_does_not_fail_the_update(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    started, release = Deferred[None](), Deferred[None]()
    applied: list[int] = []
    disposed: list[int] = []

    async def apply(ctx: Context, config: dict[str, int]) -> None:
        value = config["value"]
        applied.append(value)
        ctx.effect(lambda: lambda: disposed.append(value))
        if value != 1:
            return
        started.resolve(None)
        await release
        raise RuntimeError("superseded load")

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.use(apply, {"value": 1})
        updating = fiber.update({"value": 2})
        release.resolve(None)

    assert updating.done()
    updating.result()
    assert applied == [1, 2]
    assert disposed == [1]
    assert len([r for r in caplog.records if r.levelno >= logging.ERROR]) == 1
    assert fiber.state == FiberState.ACTIVE
    root.fiber.dispose().result()
    assert disposed == [1, 2]


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::rejects all waiting updates when the "
    "latest load fails"
)
def test_every_waiting_update_rejects_when_the_latest_load_fails(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    release = Deferred[None]()
    applied: list[int] = []
    disposed: list[int] = []

    async def apply(ctx: Context, config: dict[str, int]) -> None:
        value = config["value"]
        applied.append(value)
        ctx.effect(lambda: lambda: disposed.append(value))
        if value == 1:
            await release
        elif value == 2:
            raise RuntimeError("latest load failed")

    fiber = root.use(apply, {"value": 0})
    with caplog.at_level(logging.ERROR, logger="pycordis"):
        first = fiber.update({"value": 1})
        second = fiber.update({"value": 2})
        release.resolve(None)

    for result in (first, second):
        assert result.done()
        with pytest.raises(RuntimeError, match="latest load failed"):
            result.result()
    assert applied == [0, 1, 2]
    assert disposed == [0, 1, 2]
    assert fiber.state == FiberState.FAILED
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::stops advancing an outdated async "
    "generator and collects its final yield"
)
def test_an_outdated_async_generator_stops_after_its_last_collected_yield(
    root: Context,
) -> None:
    started, release = Deferred[None](), Deferred[None]()
    events: list[str] = []

    async def apply(ctx: Context, config: dict[str, int]) -> Any:
        events.append(f"load {config['value']}")
        if config["value"] != 1:
            return
        started.resolve(None)
        await release
        yield lambda: events.append("dispose 1")
        events.append("outdated continuation")

    fiber = root.use(apply, {"value": 1})
    updating = fiber.update({"value": 2})
    release.resolve(None)

    updating.result()
    assert events == ["load 1", "dispose 1", "load 2"]
    assert fiber.state == FiberState.ACTIVE
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::waits for dependencies and loads "
    "only the latest config"
)
def test_updates_of_a_pending_fiber_load_only_the_latest_config(root: Context) -> None:
    applied: list[int] = []

    def apply(ctx: Context, config: dict[str, int]) -> None:
        assert ctx["foo"] == "available"
        applied.append(config["value"])

    heard: list[object] = []
    root.on(
        "internal/update",
        lambda fiber, config, next_: (heard.append(config), next_())[1],
        EventOptions(is_global=True),
    )
    fiber = root.use(SimpleNamespace(inject=["foo"], apply=apply), {"value": 0})
    first = fiber.update({"value": 1})
    second = fiber.update({"value": 2})

    assert first.done() and second.done()
    # a fiber that is not loaded restarts without calling the listeners
    assert heard == []
    assert fiber.state == FiberState.PENDING
    assert applied == []
    root.provide("foo", "available")
    fiber.wait().result()

    assert applied == [2]
    assert fiber.state == FiberState.ACTIVE
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::does not restart when a listener "
    "accepts an update during loading"
)
def test_a_listener_that_takes_an_update_during_a_load_prevents_the_restart(
    root: Context,
) -> None:
    release = Deferred[None]()
    applied: list[int] = []
    received: list[int] = []

    async def apply(ctx: Context, config: dict[str, int]) -> None:
        applied.append(config["value"])
        ctx.on(
            "internal/update",
            lambda fiber, config, next_: received.append(config["value"]),
        )
        await release

    fiber = root.use(apply, {"value": 1})
    fiber.update({"value": 2}).result()
    release.resolve(None)
    fiber.wait().result()

    assert applied == [1]
    assert received == [2]
    assert fiber.config == {"value": 2}
    assert fiber.state == FiberState.ACTIVE
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "update.spec.ts::Fiber: concurrent updates::does not load a queued config after "
    "disposal"
)
def test_a_queued_update_does_not_load_after_the_fiber_is_disposed(
    root: Context,
) -> None:
    release = Deferred[None]()
    applied: list[int] = []
    disposed: list[int] = []

    async def apply(ctx: Context, config: dict[str, int]) -> Callable[[], None]:
        applied.append(config["value"])
        await release
        return lambda: disposed.append(config["value"])

    fiber = root.use(apply, {"value": 1})
    updating = fiber.update({"value": 2})
    disposing = fiber.dispose()
    release.resolve(None)

    assert updating.done() and disposing.done()
    updating.result()
    assert applied == [1]
    assert disposed == [1]
    assert fiber.state == FiberState.DISPOSED
    root.fiber.dispose().result()


@pytest.mark.parametrize("no_save", [False, True], ids=["saved", "no_save"])
def test_update_announces_the_source_config_unless_no_save_is_set(
    root: Context, no_save: bool
) -> None:
    commits: list[tuple[object, object]] = []
    root.on("internal/commit", lambda fiber, config: commits.append((fiber, config)))
    fiber = root.use(lambda ctx, config: None, "old")

    fiber.update("new", no_save=no_save).result()

    assert commits == ([] if no_save else [(fiber, "new")])
    assert fiber.config == "new"


def test_update_of_a_disposed_fiber_is_rejected(root: Context) -> None:
    fiber = root.use(lambda ctx, config: None)
    fiber.dispose().result()

    with pytest.raises(CordisError, match="inactive context"):
        fiber.update("new")


def test_update_waits_for_a_listener_that_returns_a_coroutine(
    root: Context,
) -> None:
    gate = Deferred[None]()
    applied: list[str] = []

    async def listen(fiber: Fiber, config: str, next_: Callable[[], Any]) -> None:
        await gate
        await next_()

    def apply(ctx: Context, config: str) -> None:
        applied.append(config)
        ctx.on("internal/update", listen)

    fiber = root.use(apply, "old")
    updating = fiber.update("new")

    assert not updating.done()
    assert applied == ["old"]
    gate.resolve(None)

    updating.result()
    assert applied == ["old", "new"]
