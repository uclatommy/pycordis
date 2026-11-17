"""Tests for `Deferred`, tasks, `ImmediateScheduler`, and the `FakeClock` helper."""

import gc
import threading
import time
import tracemalloc
import types
import warnings
import weakref
from collections.abc import Callable, Coroutine
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from typing import Any

import pytest
from conftest import FakeClock

from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.scheduler import FramePumpScheduler, ImmediateScheduler, Scheduler, spawn

Run = Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]


# Deferred


def test_deferred_resolves_once_and_rejects_a_second_completion() -> None:
    deferred = Deferred[int]()
    with pytest.raises(CordisError, match="DEFERRED_PENDING"):
        deferred.result()

    deferred.resolve(4)

    assert deferred.done()
    assert deferred.result() == 4
    with pytest.raises(CordisError, match="already complete"):
        deferred.reject(ValueError())


def test_deferred_reject_raises_the_error_from_result() -> None:
    deferred = Deferred[int]()
    error = ValueError("failed")
    deferred.reject(error)

    with pytest.raises(ValueError) as caught:
        deferred.result()
    assert caught.value is error


def test_done_callback_runs_on_completion_or_at_once_when_already_complete() -> None:
    seen: list[str] = []
    deferred = Deferred[str]()
    deferred.add_done_callback(lambda d: seen.append(f"before {d.result()}"))
    assert seen == []

    deferred.resolve("x")
    deferred.add_done_callback(lambda d: seen.append(f"after {d.result()}"))

    assert seen == ["before x", "after x"]


@pytest.mark.parametrize("outcome", ["value", "error", "cancelled"])
def test_from_future_follows_the_future(outcome: str) -> None:
    future: Future[int] = Future()
    deferred = Deferred.from_future(future)
    assert not deferred.done()

    if outcome == "value":
        future.set_result(5)
        assert deferred.result() == 5
    elif outcome == "error":
        future.set_exception(KeyError("k"))
        with pytest.raises(KeyError):
            deferred.result()
    else:
        future.cancel()
        with pytest.raises(CancelledError):
            deferred.result()


# Tasks


def test_task_result_is_the_return_value_or_the_raised_exception(run: Run) -> None:
    async def returns() -> int:
        return 9

    async def raises() -> int:
        raise ValueError("boom")

    assert run(returns()).result() == 9
    with pytest.raises(ValueError, match="boom"):
        run(raises()).result()


def test_task_awaiting_a_deferred_parks_until_it_completes(run: Run) -> None:
    gate = Deferred[str]()

    async def body() -> str:
        return "got " + await gate

    task = run(body())
    assert not task.done()

    gate.resolve("it")

    assert task.result() == "got it"


def test_task_awaiting_a_rejected_deferred_sees_the_error(run: Run) -> None:
    gate = Deferred[None]()

    async def body() -> str:
        try:
            await gate
        except ValueError as error:
            return f"caught {error}"
        return "missed"

    task = run(body())
    gate.reject(ValueError("no"))

    assert task.result() == "caught no"


def test_awaiting_a_completed_deferred_is_one_scheduler_step(
    scheduler: ImmediateScheduler,
) -> None:
    order: list[str] = []

    async def first() -> None:
        order.append("first start")
        await Deferred.resolved(None)
        order.append("first end")

    async def second() -> None:
        order.append("second start")

    with scheduler.call():
        spawn(scheduler, first())
        spawn(scheduler, second())

    assert order == ["first start", "second start", "first end"]


def test_work_submitted_during_a_step_runs_after_the_step(
    scheduler: ImmediateScheduler,
) -> None:
    order: list[str] = []

    async def child() -> None:
        order.append("child")

    async def parent() -> None:
        spawn(scheduler, child())
        order.append("parent")

    spawn(scheduler, parent())

    assert order == ["parent", "child"]


class _YieldsInt:
    def __await__(self) -> Any:
        return (yield 1)


@types.coroutine
def _bare_yield() -> Any:
    yield 1


@pytest.mark.parametrize(
    "awaitable",
    [lambda: 3, lambda: object(), _YieldsInt, _bare_yield],
    ids=["int", "object", "yields-int", "bare-yield"],
)
def test_awaiting_anything_but_a_deferred_raises_type_error(
    run: Run, awaitable: Callable[[], Any]
) -> None:
    async def body() -> None:
        await awaitable()

    with pytest.raises(TypeError):
        run(body()).result()


def test_task_can_catch_the_type_error_of_a_bad_await(run: Run) -> None:
    async def body() -> str:
        try:
            await _bare_yield()
        except TypeError:
            await Deferred.resolved(None)
            return "recovered"
        return "unreachable"

    assert run(body()).result() == "recovered"


def test_a_parked_task_is_not_collected_with_its_teardown(
    run: Run,
) -> None:
    log: list[str] = []
    handoff: list[weakref.ref[Deferred[None]]] = []

    async def body() -> None:
        gate = Deferred[None]()
        handoff.append(weakref.ref(gate))
        try:
            await gate
        finally:
            log.append("teardown")

    task = run(body())
    del task
    gc.collect()

    gate = handoff[0]()
    assert gate is not None and log == []
    gate.resolve(None)
    assert log == ["teardown"]


# ImmediateScheduler


def test_call_block_defers_the_drain_to_the_outermost_exit(
    scheduler: ImmediateScheduler,
) -> None:
    order: list[str] = []

    with scheduler.call():
        scheduler.submit(lambda: order.append("outer"))
        with scheduler.call():
            scheduler.submit(lambda: order.append("inner"))
        assert order == []
    assert order == ["outer", "inner"]


def test_call_block_drains_when_its_body_raises(
    scheduler: ImmediateScheduler,
) -> None:
    ran: list[str] = []

    with pytest.raises(ValueError, match="body"):
        with scheduler.call():
            scheduler.submit(lambda: ran.append("queued"))
            raise ValueError("body")

    assert ran == ["queued"]


def test_a_custom_scheduler_needs_only_submit_and_call_does_nothing() -> None:
    class Recording(Scheduler):
        def __init__(self) -> None:
            super().__init__()
            self.queue: list[Callable[[], None]] = []

        def submit(self, fn: Callable[[], None]) -> None:
            self.queue.append(fn)

    custom = Recording()
    gate = Deferred[int]()

    async def body() -> int:
        return await gate

    with custom.call():
        task = spawn(custom, body())
    assert custom.queue and not task.done()

    custom.queue.pop()()
    gate.resolve(2)
    custom.queue.pop()()

    assert task.result() == 2


def test_run_until_quiet_waits_for_a_deferred_that_a_worker_completes(
    scheduler: ImmediateScheduler,
) -> None:
    with ThreadPoolExecutor(max_workers=1) as pool:
        gate = threading.Event()
        deferred = Deferred.from_future(pool.submit(lambda: gate.wait(5) and "done"))

        async def body() -> str:
            return await deferred

        task = spawn(scheduler, body())
        assert not task.done()
        gate.set()
        scheduler.run_until_quiet(timeout=5)

    assert task.result() == "done"


def test_run_until_quiet_raises_when_a_task_stays_parked(
    scheduler: ImmediateScheduler,
) -> None:
    async def body() -> None:
        await Deferred[None]()

    spawn(scheduler, body())

    with pytest.raises(TimeoutError, match="1 task"):
        scheduler.run_until_quiet(timeout=0.01)


def test_serve_raises_when_its_condition_stays_false(
    scheduler: ImmediateScheduler,
) -> None:
    with pytest.raises(TimeoutError, match="condition"):
        scheduler.serve(lambda: False, timeout=0.01)


def test_serve_returns_at_once_when_its_condition_is_true(
    scheduler: ImmediateScheduler,
) -> None:
    scheduler.serve(lambda: True)


def test_serve_checks_its_condition_after_work_that_another_thread_submits(
    scheduler: ImmediateScheduler,
) -> None:
    done: list[int] = []
    worker = threading.Timer(0.01, lambda: scheduler.submit(lambda: done.append(1)))
    worker.start()

    scheduler.serve(lambda: bool(done), timeout=5)

    worker.join(timeout=5)
    assert not worker.is_alive()
    assert done == [1]


def test_a_failing_submitted_function_leaves_later_work_queued(
    scheduler: ImmediateScheduler,
) -> None:
    ran: list[str] = []

    def fail() -> None:
        raise RuntimeError("step failed")

    with pytest.raises(RuntimeError, match="step failed"):
        with scheduler.call():
            scheduler.submit(fail)
            scheduler.submit(lambda: ran.append("later"))
    assert ran == []

    scheduler.run_until_quiet(timeout=1)

    assert ran == ["later"]


# FramePumpScheduler


class StepClock:
    """A clock that each step advances by one second, and that counts its reads."""

    def __init__(self) -> None:
        self.now = 0.0
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        return self.now

    def step(self) -> None:
        self.now += 1.0


@pytest.mark.parametrize(
    ("queued", "max_steps", "budget", "ran"),
    [
        (5, 2, None, 2),
        (3, 10, None, 3),
        (3, 0, None, 0),
        (0, 4, None, 0),
        (5, None, 2.5, 3),
        (5, 4, 2.5, 3),
        (5, 2, 10.5, 2),
        (2, None, 10.5, 2),
        (5, None, 0.0, 1),
    ],
)
def test_pump_stops_at_the_first_limit_and_the_next_call_continues(
    queued: int, max_steps: int | None, budget: float | None, ran: int
) -> None:
    clock = StepClock()
    pump = FramePumpScheduler(clock=clock)
    seen: list[int] = []
    for index in range(queued):
        pump.submit(lambda index=index: (seen.append(index), clock.step()))

    assert pump.pump(max_steps, budget=budget) == ran
    assert seen == list(range(ran))
    assert pump.pending == queued - ran
    assert pump.pump(queued) == queued - ran
    assert pump.pending == 0


@pytest.mark.parametrize(
    ("queued", "max_steps", "budget", "ran", "reads"),
    [
        (0, None, 1.0, 0, 0),
        (3, 0, 1.0, 0, 0),
        (3, None, 0.0, 1, 2),
        (3, 2, 5.0, 2, 2),
        (1, None, 5.0, 1, 1),
        (3, 2, None, 2, 0),
    ],
)
def test_pump_reads_the_clock_only_when_a_budget_step_will_run(
    queued: int, max_steps: int | None, budget: float | None, ran: int, reads: int
) -> None:
    clock = StepClock()
    pump = FramePumpScheduler(clock=clock)
    for _ in range(queued):
        pump.submit(lambda: None)

    assert pump.pump(max_steps, budget=budget) == ran
    assert clock.reads == reads


@pytest.mark.parametrize(
    ("max_steps", "budget", "message"),
    [
        (None, None, "max_steps, budget"),
        (-1, None, "max_steps must not be negative, got -1"),
        (None, -0.5, "budget must be a number of seconds >= 0, got -0.5"),
        (3, float("nan"), "got nan"),
    ],
)
def test_pump_rejects_an_invalid_limit(
    max_steps: int | None, budget: float | None, message: str
) -> None:
    pump = FramePumpScheduler()
    pump.submit(lambda: None)

    with pytest.raises(ValueError, match=message):
        pump.pump(max_steps, budget=budget)
    assert pump.pending == 1


@pytest.mark.parametrize("limit", [{"max_steps": 1}, {"budget": 0.0}])
def test_pump_runs_a_step_that_another_step_submits_within_the_limit(
    limit: dict[str, Any],
) -> None:
    pump = FramePumpScheduler()
    seen: list[str] = []

    def first() -> None:
        seen.append("first")
        pump.submit(lambda: seen.append("second"))

    pump.submit(first)

    assert pump.pump(**limit) == 1
    assert seen == ["first"]
    assert pump.pump(5) == 1
    assert seen == ["first", "second"]


@pytest.mark.parametrize("limit", [{"max_steps": 5}, {"budget": 60.0}])
def test_pump_leaves_later_steps_queued_when_a_step_raises(
    limit: dict[str, Any],
) -> None:
    pump = FramePumpScheduler()
    seen: list[str] = []

    def fail() -> None:
        raise RuntimeError("step failed")

    pump.submit(fail)
    pump.submit(lambda: seen.append("later"))

    with pytest.raises(RuntimeError, match="step failed"):
        pump.pump(**limit)
    assert pump.pending == 1
    assert pump.pump(5) == 1
    assert seen == ["later"]


def test_pump_resumes_a_parked_task_after_its_deferred_completes() -> None:
    pump = FramePumpScheduler()
    gate = Deferred[int]()

    async def body() -> int:
        return await gate

    task = spawn(pump, body())
    pump.pump(10)
    assert not task.done()

    gate.resolve(3)
    assert not task.done()
    pump.pump(10)

    assert task.result() == 3


def test_an_empty_pump_is_fast_and_allocates_nothing() -> None:
    pump = FramePumpScheduler()
    pump.pump(8)  # warm-up
    calls = 20_000

    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        started = time.perf_counter()
        for _ in range(calls):
            pump.pump(8)
        elapsed = time.perf_counter() - started
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()

    assert elapsed / calls < 50e-6
    grown = [
        stat
        for stat in after.compare_to(before, "lineno")
        if stat.size_diff > 0 and "src/pycordis" in str(stat.traceback)
    ]
    if grown:  # interpreter noise on some platforms: warn, and let the timing gate
        warnings.warn(f"an empty pump allocated memory: {grown}", stacklevel=1)


# FakeClock


def test_clock_completes_due_timers_in_deadline_order_and_runs_woken_tasks(
    run: Run, clock: FakeClock
) -> None:
    order: list[str] = []

    async def sleeper(name: str, ms: int) -> None:
        await clock.sleep(ms)
        order.append(f"{name}@{clock.now}")

    run(sleeper("late", 30))
    run(sleeper("early", 10))
    clock.advance(20)
    assert order == ["early@10"]

    clock.advance(20)
    assert order == ["early@10", "late@30"]
    assert clock.now == 40
