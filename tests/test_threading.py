"""Owner-thread rules: which calls a second thread can make."""

import threading
from collections.abc import Callable
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.scheduler import FramePumpScheduler, ImmediateScheduler, Scheduler, spawn

JOIN_TIMEOUT = 5


def call_on_thread(fn: Callable[[], Any]) -> tuple[Any, BaseException | None]:
    """Run `fn` on a new thread. Return its result and its exception."""
    outcome: list[Any] = [None, None]

    def target() -> None:
        try:
            outcome[0] = fn()
        except BaseException as error:  # noqa: BLE001 - the test reports it
            outcome[1] = error

    worker = threading.Thread(target=target)
    worker.start()
    worker.join(timeout=JOIN_TIMEOUT)
    assert not worker.is_alive()
    return outcome[0], outcome[1]


def drain(scheduler: Scheduler) -> None:
    """Run the work that other threads submitted."""
    if isinstance(scheduler, FramePumpScheduler):
        scheduler.pump(100)
    else:
        assert isinstance(scheduler, ImmediateScheduler)
        scheduler.run_until_quiet(timeout=1)


@pytest.fixture(params=[ImmediateScheduler, FramePumpScheduler])
def any_scheduler(request: pytest.FixtureRequest) -> Scheduler:
    return request.param()


@pytest.mark.divergence("owner-thread")
@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: ctx.use(lambda ctx, config: None),
        lambda ctx: ctx.get("missing", strict=False),
        lambda ctx: ctx.provide("service", 1),
        lambda ctx: ctx.emit("custom-event"),
        lambda ctx: ctx.on("custom-event", lambda: None),
        lambda ctx: ctx.effect(lambda: None),
        lambda ctx: ctx.fiber.get_effects(),
        lambda ctx: ctx.fiber.dispose(),
        lambda ctx: ctx.registry.has(print),
        lambda ctx: ctx.events.listener_counts(),
        lambda ctx: ctx.reflect.has_property("service"),
    ],
    ids=[
        "use",
        "get",
        "provide",
        "emit",
        "on",
        "effect",
        "fiber.get_effects",
        "fiber.dispose",
        "registry.has",
        "events.listener_counts",
        "reflect.has_property",
    ],
)
def test_a_call_from_a_second_thread_raises_wrong_thread(
    root: Context, call: Callable[[Context], Any]
) -> None:
    effects_before = len(root.fiber.get_effects())

    _, error = call_on_thread(lambda: call(root))

    assert isinstance(error, CordisError)
    assert str(error) == "WRONG_THREAD"
    assert len(root.fiber.get_effects()) == effects_before


@pytest.mark.divergence("owner-thread")
def test_the_scheduler_drivers_reject_a_second_thread(any_scheduler: Scheduler) -> None:
    drive = (
        (lambda: any_scheduler.pump(1))
        if isinstance(any_scheduler, FramePumpScheduler)
        else any_scheduler.run_until_quiet  # type: ignore[attr-defined]
    )

    _, error = call_on_thread(drive)

    assert isinstance(error, CordisError)
    assert str(error) == "WRONG_THREAD"


@pytest.mark.divergence("owner-thread")
def test_submit_from_a_second_thread_runs_on_the_owner_thread(
    any_scheduler: Scheduler,
) -> None:
    ran_on: list[int] = []

    _, error = call_on_thread(
        lambda: any_scheduler.submit(lambda: ran_on.append(threading.get_ident()))
    )
    assert error is None
    assert ran_on == []

    drain(any_scheduler)

    assert ran_on == [threading.get_ident()]


@pytest.mark.divergence("owner-thread")
def test_a_deferred_completed_on_a_worker_resumes_its_task_on_the_owner_thread(
    any_scheduler: Scheduler,
) -> None:
    gate = Deferred[str]()
    resumed_on: list[int] = []

    async def body() -> str:
        value = await gate
        resumed_on.append(threading.get_ident())
        return value

    task = spawn(any_scheduler, body())
    if isinstance(any_scheduler, FramePumpScheduler):
        any_scheduler.pump(100)  # an immediate scheduler has run the first step
    assert not task.done()

    _, error = call_on_thread(lambda: gate.resolve("done"))
    assert error is None
    assert not task.done()
    drain(any_scheduler)

    assert task.result() == "done"
    assert resumed_on == [threading.get_ident()]
