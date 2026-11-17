"""The timer service and its clocks."""

import logging
import random
import threading
from collections.abc import Callable
from contextlib import aclosing
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import Fiber
from pycordis.scheduler import FramePumpScheduler, ImmediateScheduler, spawn
from pycordis.timer import CLOCK, ManualClock, ThreadingClock, Ticks, TimerService

type Factory = Callable[[Context, Callable[..., Any]], Callable[..., Any]]


def idle(ctx: Context, config: object) -> None:
    """A plugin that injects the timer service and does nothing else."""


idle.inject = ["timer"]  # type: ignore[attr-defined]


@pytest.fixture
def manual(root: Context) -> ManualClock:
    """A `ManualClock` that the root provides, with the timer service in use."""
    clock = ManualClock()
    root.provide(CLOCK, clock)
    root.use(TimerService)
    return clock


@pytest.fixture
def plugin(root: Context, manual: ManualClock) -> Fiber:
    """The fiber of a plugin that injects the timer service."""
    return root.use(idle)


class Calls:
    """A callback that counts its calls."""

    def __init__(self) -> None:
        self.args: list[tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.args.append(args)

    def __len__(self) -> int:
        return len(self.args)


@pytest.fixture
def calls() -> Calls:
    return Calls()


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.timeout()::basic support")
def test_timeout_calls_the_callback_once_at_its_deadline(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    plugin.ctx["timeout"](calls, 1.0)
    assert len(calls) == 0
    manual.advance(1.0)
    assert len(calls) == 1
    manual.advance(1.0)
    assert len(calls) == 1


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.timeout()::dispose")
def test_timeout_does_not_fire_after_its_handle_is_called(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    handle = plugin.ctx["timeout"](calls, 1.0)
    assert len(calls) == 0
    handle()
    manual.advance(2.0)
    assert len(calls) == 0


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.interval()::basic support")
def test_interval_calls_the_callback_until_its_handle_is_called(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    handle = plugin.ctx["interval"](calls, 1.0)
    assert len(calls) == 0
    manual.advance(1.0)
    assert len(calls) == 1
    manual.advance(1.0)
    assert len(calls) == 2
    handle()
    manual.advance(2.0)
    assert len(calls) == 2


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.throttle()::basic support")
def test_throttle_runs_the_first_call_at_once_and_a_late_call_after_the_delay(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    throttled = plugin.ctx["throttle"](calls, 1.0)
    throttled()
    assert len(calls) == 1
    manual.advance(0.75)
    throttled()
    assert len(calls) == 1
    manual.advance(0.75)
    assert len(calls) == 2
    throttled()
    manual.advance(2.0)
    assert len(calls) == 3


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.throttle()::trailing mode")
def test_throttle_runs_one_trailing_call_with_the_newest_arguments(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    throttled = plugin.ctx["throttle"](calls, 1.0)
    throttled("first")
    manual.advance(0.5)
    throttled("second")
    throttled("third")
    assert calls.args == [("first",)]
    manual.advance(0.5)
    assert calls.args == [("first",), ("third",)]
    manual.advance(2.0)
    assert len(calls) == 2


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.throttle()::no trailing mode"
)
def test_throttle_without_trailing_drops_a_call_that_comes_too_early(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    throttled = plugin.ctx["throttle"](calls, 1.0, trailing=False)
    throttled()
    manual.advance(0.5)
    throttled()
    assert len(calls) == 1
    manual.advance(2.0)
    assert len(calls) == 1
    throttled()
    assert len(calls) == 2


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.debounce()::basic support")
def test_debounce_calls_the_callback_after_the_last_call_and_the_quiet_time(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    debounced = plugin.ctx["debounce"](calls, 1.0)
    debounced("a")
    manual.advance(0.25)
    debounced("b")
    manual.advance(0.25)
    debounced("c")
    assert len(calls) == 0
    manual.advance(1.0)
    assert calls.args == [("c",)]
    keyed: list[dict[str, int]] = []
    plugin.ctx["debounce"](lambda **kwargs: keyed.append(kwargs), 1.0)(x=1)
    manual.advance(1.0)
    assert keyed == [{"x": 1}]


SCHEDULED: dict[str, Factory] = {
    "throttle": lambda ctx, callback: ctx["throttle"](callback, 1.0),
    "throttle-no-trailing": lambda ctx, callback: ctx["throttle"](
        callback, 1.0, trailing=False
    ),
    "debounce": lambda ctx, callback: ctx["debounce"](callback, 1.0),
}


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.throttle()::disposed")
@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.throttle()::disposed with noTrailing"
)
@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.debounce()::disposed")
@pytest.mark.parametrize("kind", SCHEDULED)
def test_a_disposed_scheduled_function_does_nothing(
    kind: str, plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    scheduled = SCHEDULED[kind](plugin.ctx, calls)
    scheduled.dispose()
    scheduled()
    manual.advance(0.5)
    scheduled()
    manual.advance(2.0)
    scheduled()
    assert len(calls) == 0


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.throttle()::context dispose")
@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.debounce()::context dispose")
@pytest.mark.parametrize(("kind", "leading"), [("throttle", 1), ("debounce", 0)])
def test_a_scheduled_function_does_nothing_after_its_fiber_unloads(
    kind: str,
    leading: int,
    plugin: Fiber,
    manual: ManualClock,
    calls: Calls,
) -> None:
    scheduled = SCHEDULED[kind](plugin.ctx, calls)
    scheduled()
    assert len(calls) == leading
    manual.advance(0.5)
    scheduled()
    plugin.dispose()
    manual.advance(2.0)
    assert len(calls) == leading
    scheduled()
    assert len(calls) == leading


@pytest.mark.contract("EFF-REVERT")
def test_a_timer_is_an_effect_of_its_fiber_with_a_label(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    ctx = plugin.ctx
    ctx["timeout"](calls, 1.0)
    ctx["interval"](calls, 4.0)
    ctx["throttle"](calls, 1.0)
    ctx["debounce"](calls, 1.0)
    labels = lambda: [meta.label for meta in plugin.get_effects()]  # noqa: E731
    assert labels() == [
        "ctx.timeout()",
        "ctx.interval()",
        "ctx.throttle()",
        "ctx.debounce()",
    ]

    manual.advance(1.0)
    assert len(calls) == 1
    assert "ctx.timeout()" not in labels()

    plugin.dispose()
    manual.advance(10.0)
    assert len(calls) == 1


def test_the_handle_of_a_fired_timeout_does_nothing(
    plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    handle = plugin.ctx["timeout"](calls, 1.0)
    manual.advance(1.0)
    assert handle() is None
    assert len(calls) == 1


def test_interval_ticks_at_multiples_of_the_delay_whatever_the_step(
    plugin: Fiber, manual: ManualClock
) -> None:
    times: list[float] = []
    plugin.ctx["interval"](lambda: times.append(manual.now()), 0.5)
    manual.advance(2.25)
    assert times == [0.5, 1.0, 1.5, 2.0]
    manual.advance(0.25)
    assert times == [0.5, 1.0, 1.5, 2.0, 2.5]


def test_a_callback_can_stop_its_own_interval(
    plugin: Fiber, manual: ManualClock
) -> None:
    ticks: list[int] = []

    def tick() -> None:
        ticks.append(1)
        if len(ticks) == 2:
            handle()

    handle = plugin.ctx["interval"](tick, 1.0)
    manual.advance(10.0)
    assert len(ticks) == 2
    assert plugin.get_effects() == []


@pytest.mark.parametrize(
    "method", ["timeout", "interval", "throttle", "debounce", "sleep", "ticks"]
)
@pytest.mark.parametrize("delay", [-1.0, float("nan")])
def test_a_timer_method_rejects_a_negative_delay(
    method: str, delay: float, plugin: Fiber
) -> None:
    args = () if method in ("sleep", "ticks") else (lambda: None,)
    with pytest.raises(ValueError, match="delay"):
        plugin.ctx[method](*args, delay)
    assert plugin.get_effects() == []


@pytest.mark.parametrize("method", ["interval", "ticks"])
def test_a_periodic_timer_rejects_a_delay_of_zero(method: str, plugin: Fiber) -> None:
    args = () if method == "ticks" else (lambda: None,)
    with pytest.raises(ValueError, match="positive"):
        plugin.ctx[method](*args, 0)


@pytest.mark.parametrize("method", ["timeout", "throttle", "debounce"])
def test_a_delay_of_zero_is_due_now(
    method: str, plugin: Fiber, manual: ManualClock, calls: Calls
) -> None:
    scheduled = plugin.ctx[method](calls, 0)
    if method != "timeout":
        scheduled()
    manual.advance(0)
    assert len(calls) == 1


def test_a_sleep_of_zero_is_due_now(plugin: Fiber, manual: ManualClock) -> None:
    waiting = plugin.ctx["sleep"](0)
    manual.advance(0)
    assert waiting.done()


def failing() -> None:
    raise RuntimeError("boom")


@pytest.mark.divergence("timer-callback-errors")
@pytest.mark.contract("EFF-ISOLATE")
@pytest.mark.parametrize("kind", ["timeout", "interval", "throttle", "debounce"])
def test_a_callback_error_is_logged_and_the_other_timers_continue(
    kind: str,
    root: Context,
    plugin: Fiber,
    manual: ManualClock,
    calls: Calls,
    caplog: pytest.LogCaptureFixture,
) -> None:
    other = root.use(idle)
    other.ctx["timeout"](calls, 1.0)
    ctx = plugin.ctx
    if kind in ("timeout", "interval"):
        ctx[kind](failing, 1.0)
    else:
        scheduled = ctx[kind](failing, 1.0)
        if kind == "throttle":
            with pytest.raises(RuntimeError):  # the leading call runs in the caller
                scheduled()
        scheduled()  # a trailing or debounced call fails on the clock

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        manual.advance(1.0)

    assert len(calls) == 1
    [record] = [r for r in caplog.records if r.exc_info]
    assert record.exc_info is not None
    assert str(record.exc_info[1]) == "boom"
    assert record.name == f"pycordis.{plugin.name}"


@pytest.mark.divergence("timer-callback-errors")
def test_an_interval_continues_after_a_callback_error(
    plugin: Fiber, manual: ManualClock, caplog: pytest.LogCaptureFixture
) -> None:
    attempts: list[int] = []

    def flaky() -> None:
        attempts.append(1)
        raise RuntimeError("boom")

    plugin.ctx["interval"](flaky, 1.0)
    with caplog.at_level(logging.ERROR, logger="pycordis"):
        manual.advance(3.0)
    assert len(attempts) == 3
    assert len([r for r in caplog.records if r.exc_info]) == 3


def test_a_change_of_clock_reloads_the_timer_service_and_stops_the_old_timers(
    root: Context, calls: Calls
) -> None:
    first, second = ManualClock(), ManualClock()
    remove = root.provide(CLOCK, first)
    root.use(TimerService)
    loads: list[int] = []

    def starter(ctx: Context, config: object) -> None:
        loads.append(1)
        ctx["timeout"](calls, 1.0)

    starter.inject = ["timer"]  # type: ignore[attr-defined]
    root.use(starter)

    remove()
    root.provide(CLOCK, second)
    first.advance(2.0)
    assert (len(loads), len(calls)) == (2, 0)
    second.advance(2.0)
    assert len(calls) == 1


def test_a_clock_runs_the_calls_in_deadline_order_with_ties_in_call_order() -> None:
    clock = ManualClock()
    order: list[str] = []
    clock.call_later(2.0, lambda: order.append("late"))
    clock.call_later(1.0, lambda: order.append("first"))
    clock.call_later(1.0, lambda: order.append("second"))
    clock.advance(5.0)
    assert order == ["first", "second", "late"]


def test_a_call_that_a_callback_schedules_runs_in_the_same_advance() -> None:
    clock = ManualClock()
    seen: list[tuple[str, float]] = []

    def outer() -> None:
        seen.append(("outer", clock.now()))
        clock.call_later(0.5, lambda: seen.append(("inner", clock.now())))
        clock.call_later(-1.0, lambda: seen.append(("now", clock.now())))
        clock.call_later(10.0, lambda: seen.append(("far", clock.now())))

    clock.call_later(1.0, outer)
    clock.advance(2.0)
    assert seen == [("outer", 1.0), ("now", 1.0), ("inner", 1.5)]
    assert clock.now() == 2.0


def test_a_cancelled_call_does_not_run_and_a_negative_step_is_rejected() -> None:
    clock = ManualClock(start=3.0)
    ran: list[int] = []
    cancel = clock.call_later(1.0, lambda: ran.append(1))
    cancel()
    clock.advance(2.0)
    assert (ran, clock.now()) == ([], 5.0)
    with pytest.raises(ValueError, match="negative"):
        clock.advance(-0.5)


@pytest.fixture
def threading_clock() -> Callable[[ImmediateScheduler | FramePumpScheduler], Any]:
    """Create `ThreadingClock` objects, and close them when the test ends."""
    clocks: list[ThreadingClock] = []

    def create(scheduler: ImmediateScheduler | FramePumpScheduler) -> ThreadingClock:
        clock = ThreadingClock(scheduler)
        clocks.append(clock)
        return clock

    yield create
    for clock in clocks:
        clock.close(timeout=5)


def test_a_threading_clock_runs_the_callback_on_the_owner_thread(
    scheduler: ImmediateScheduler, threading_clock: Callable[..., ThreadingClock]
) -> None:
    clock = threading_clock(scheduler)
    done = Deferred[None]()
    threads: list[int] = []

    def callback() -> None:
        threads.append(threading.get_ident())
        done.resolve(None)

    async def wait() -> None:
        await done

    spawn(scheduler, wait())
    clock.call_later(0.01, callback)
    scheduler.run_until_quiet(timeout=5)
    assert threads == [threading.get_ident()]


class SignallingScheduler(FramePumpScheduler):
    """A scheduler that tells a test when the clock thread has submitted a step."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted = threading.Event()

    def submit(self, fn: Callable[[], None]) -> None:
        super().submit(fn)
        self.submitted.set()


def test_a_threading_clock_does_not_run_a_call_that_is_cancelled_after_it_is_due(
    threading_clock: Callable[..., ThreadingClock],
) -> None:
    scheduler = SignallingScheduler()
    clock = threading_clock(scheduler)
    ran: list[int] = []
    cancel = clock.call_later(0, lambda: ran.append(1))
    assert scheduler.submitted.wait(timeout=5)
    cancel()
    assert scheduler.pump(10) == 1
    assert ran == []


def test_a_threading_clock_cancel_before_the_deadline_stops_the_call(
    scheduler: ImmediateScheduler, threading_clock: Callable[..., ThreadingClock]
) -> None:
    clock = threading_clock(scheduler)
    ran: list[str] = []
    clock.call_later(0.01, lambda: ran.append("cancelled"))()
    clock.call_later(0.02, lambda: ran.append("kept"))
    done = Deferred[None]()
    clock.call_later(0.03, lambda: done.resolve(None))

    async def wait() -> None:
        await done

    spawn(scheduler, wait())
    scheduler.run_until_quiet(timeout=5)
    assert ran == ["kept"]


def test_a_threading_clock_logs_a_scheduler_failure_and_keeps_running(
    threading_clock: Callable[..., ThreadingClock],
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Refusing(SignallingScheduler):
        def __init__(self) -> None:
            super().__init__()
            self.refused = threading.Event()

        def submit(self, fn: Callable[[], None]) -> None:
            if not self.refused.is_set():
                self.refused.set()
                raise RuntimeError("refused")
            super().submit(fn)

    scheduler = Refusing()
    clock = threading_clock(scheduler)
    ran: list[int] = []
    with caplog.at_level(logging.ERROR, logger="pycordis.timer"):
        clock.call_later(0, lambda: ran.append(0))
        assert scheduler.refused.wait(timeout=5)
        clock.call_later(0, lambda: ran.append(1))
        assert scheduler.submitted.wait(timeout=5)
    scheduler.pump(10)
    assert ran == [1]
    assert [r.message for r in caplog.records] == [
        "the scheduler did not accept a timer call"
    ]
    assert isinstance(clock.now(), float)


def test_close_reports_a_worker_thread_that_does_not_stop_in_time() -> None:
    class Blocking(SignallingScheduler):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()

        def submit(self, fn: Callable[[], None]) -> None:
            self.submitted.set()
            assert self.release.wait(timeout=5)

    scheduler = Blocking()
    clock = ThreadingClock(scheduler)
    clock.call_later(0, lambda: None)
    assert scheduler.submitted.wait(timeout=5)
    with pytest.raises(TimeoutError):
        clock.close(timeout=0.01)
    scheduler.release.set()
    clock.close(timeout=5)


def test_a_closed_threading_clock_stops_its_thread_and_rejects_new_calls(
    scheduler: ImmediateScheduler,
) -> None:
    clock = ThreadingClock(scheduler)
    ran: list[int] = []
    clock.call_later(60.0, lambda: ran.append(1))

    clock.close(timeout=5)
    clock.close(timeout=5)

    assert not [t for t in threading.enumerate() if t.name == "pycordis-clock"]
    with pytest.raises(CordisError, match="closed"):
        clock.call_later(1.0, lambda: None)
    assert ran == []


def test_a_threading_clock_drives_the_timer_service(
    root: Context,
    scheduler: ImmediateScheduler,
    threading_clock: Callable[..., ThreadingClock],
) -> None:
    root.provide(CLOCK, threading_clock(scheduler))
    root.use(TimerService)
    fiber = root.use(idle)
    done = Deferred[None]()
    fiber.ctx["timeout"](lambda: done.resolve(None), 0.01)

    async def wait() -> None:
        await done

    spawn(scheduler, wait())
    scheduler.run_until_quiet(timeout=5)
    assert done.done()
    assert fiber.get_effects() == []


# -- wait forms ------------------------------------------------------------------------


@pytest.mark.upstream("timer/index.spec.ts::ctx.timer::ctx.timeout()::promise")
def test_sleep_resolves_at_its_deadline_and_a_later_unload_does_not_reject_it(
    plugin: Fiber, manual: ManualClock
) -> None:
    waiting = plugin.ctx["sleep"](1.0)
    manual.advance(0.5)
    assert not waiting.done()
    manual.advance(0.5)
    assert waiting.result() is None
    assert plugin.get_effects() == []
    plugin.dispose()
    manual.advance(2.0)
    assert waiting.result() is None


@pytest.mark.contract("EFF-REVERT")
def test_a_pending_sleep_rejects_when_its_fiber_unloads(
    plugin: Fiber, manual: ManualClock
) -> None:
    waiting = plugin.ctx["sleep"](1.0)
    assert [meta.label for meta in plugin.get_effects()] == ["ctx.timeout()"]
    plugin.dispose()
    with pytest.raises(CordisError, match="Context has been disposed"):
        waiting.result()
    manual.advance(2.0)


def read_ticks(
    scheduler: ImmediateScheduler, ticks: Ticks, counter: Calls
) -> Deferred[None]:
    """Start a task that reads `ticks` with `async for` and counts each item."""

    async def iterate() -> None:
        async for _ in ticks:
            counter()

    return spawn(scheduler, iterate())


@pytest.fixture
def ticks(plugin: Fiber) -> Ticks:
    return plugin.ctx["ticks"](1.0)


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator (manual return)"
)
def test_aclose_ends_an_async_for_loop_and_stops_the_timer(
    plugin: Fiber,
    manual: ManualClock,
    scheduler: ImmediateScheduler,
    ticks: Ticks,
    calls: Calls,
) -> None:
    loop = read_ticks(scheduler, ticks, calls)
    manual.advance(1.0)
    manual.advance(1.0)
    assert len(calls) == 2
    assert ticks.aclose().result() is None
    manual.advance(1.0)
    assert len(calls) == 2
    assert loop.result() is None
    assert plugin.get_effects() == []


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator (manual throw)"
)
def test_athrow_ends_an_async_for_loop_with_the_error(
    manual: ManualClock,
    scheduler: ImmediateScheduler,
    ticks: Ticks,
    calls: Calls,
) -> None:
    loop = read_ticks(scheduler, ticks, calls)
    manual.advance(1.0)
    manual.advance(1.0)
    assert len(calls) == 2
    reason = ValueError("test")
    with pytest.raises(StopAsyncIteration):
        ticks.athrow(reason).result()
    manual.advance(1.0)
    assert len(calls) == 2
    with pytest.raises(ValueError, match="test"):
        loop.result()


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator (concurrent reads)"
)
def test_a_tick_resolves_the_oldest_pending_read(
    manual: ManualClock, ticks: Ticks
) -> None:
    first, second = ticks.__anext__(), ticks.__anext__()
    manual.advance(1.0)
    assert (first.done(), second.done()) == (True, False)
    manual.advance(1.0)
    assert (first.result(), second.result()) == (None, None)


def test_a_tick_with_no_pending_read_is_lost(manual: ManualClock, ticks: Ticks) -> None:
    manual.advance(3.0)
    read = ticks.__anext__()
    assert not read.done()
    manual.advance(1.0)
    assert read.done()


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator "
    "(return with concurrent reads)"
)
def test_aclose_rejects_pending_and_later_reads_with_stop_async_iteration(
    ticks: Ticks,
) -> None:
    reads = [ticks.__anext__() for _ in range(3)]
    assert ticks.aclose().result() is None
    for read in [*reads, ticks.__anext__()]:
        with pytest.raises(StopAsyncIteration):
            read.result()


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator "
    "(throw with concurrent reads)"
)
def test_athrow_rejects_pending_and_later_reads_with_the_error(
    plugin: Fiber, ticks: Ticks
) -> None:
    assert [meta.label for meta in plugin.get_effects()] == ["ctx.interval()"]
    reads = [ticks.__anext__() for _ in range(3)]
    reason = ValueError("test")
    with pytest.raises(StopAsyncIteration):
        ticks.athrow(reason).result()
    for read in [*reads, ticks.__anext__()]:
        with pytest.raises(ValueError) as info:
            read.result()
        assert info.value is reason
    assert plugin.get_effects() == []


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("close", "throw", StopAsyncIteration),
        ("throw", "close", ValueError),
        ("unload", "close", CordisError),
        ("unload", "throw", CordisError),
    ],
)
def test_the_first_end_of_ticks_stays(
    first: str, second: str, expected: type[BaseException], plugin: Fiber, ticks: Ticks
) -> None:
    ends: dict[str, Callable[[], object]] = {
        "close": ticks.aclose,
        "throw": lambda: ticks.athrow(ValueError("test")),
        "unload": plugin.dispose,
    }
    ends[first]()
    ends[second]()
    with pytest.raises(expected):
        ticks.__anext__().result()


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator (break return)"
)
@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator (break throw)"
)
@pytest.mark.parametrize("exit_kind", ["break", "raise"])
def test_aclosing_stops_the_timer_when_a_loop_exits_early(
    exit_kind: str,
    plugin: Fiber,
    manual: ManualClock,
    scheduler: ImmediateScheduler,
    calls: Calls,
) -> None:
    async def iterate() -> None:
        seen = 0
        async with aclosing(plugin.ctx["ticks"](1.0)) as ticks:
            async for _ in ticks:
                seen += 1
                if seen > 2:
                    if exit_kind == "raise":
                        raise ValueError("test")
                    break
                calls()

    loop = spawn(scheduler, iterate())
    for _ in range(3):
        manual.advance(1.0)
    assert len(calls) == 2
    assert plugin.get_effects() == []
    if exit_kind == "raise":
        with pytest.raises(ValueError, match="test"):
            loop.result()
    else:
        assert loop.result() is None


@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator (context dispose)"
)
@pytest.mark.upstream(
    "timer/index.spec.ts::ctx.timer::ctx.interval()::async iterator "
    "(context dispose with concurrent reads)"
)
def test_a_fiber_unload_rejects_pending_and_later_reads(
    plugin: Fiber,
    manual: ManualClock,
    scheduler: ImmediateScheduler,
    ticks: Ticks,
    calls: Calls,
) -> None:
    loop = read_ticks(scheduler, ticks, calls)
    manual.advance(1.0)
    manual.advance(1.0)
    assert len(calls) == 2
    reads = [ticks.__anext__() for _ in range(3)]
    plugin.dispose()
    manual.advance(1.0)
    assert len(calls) == 2
    for read in [*reads, ticks.__anext__()]:
        with pytest.raises(CordisError, match="Context has been disposed"):
            read.result()
    with pytest.raises(CordisError, match="Context has been disposed"):
        loop.result()


@pytest.mark.contract("EFF-REVERT")
@pytest.mark.parametrize("seed", range(25))
def test_no_timer_of_a_disposed_fiber_fires_in_a_random_schedule(
    seed: int, root: Context
) -> None:
    rng = random.Random(seed)
    clock = ManualClock()
    root.provide(CLOCK, clock)
    root.use(TimerService)
    live: dict[int, Fiber] = {}
    disposed: set[int] = set()
    log: list[str] = []
    next_id = 0

    def on_resolve(result: Deferred[None], callback: Callable[[], None]) -> None:
        """Call `callback` when `result` resolves. A rejection is not a firing."""

        def done(finished: Deferred[None]) -> None:
            try:
                finished.result()
            except CordisError:
                return
            callback()

        result.add_done_callback(done)

    def watch(owner: int, kind: str) -> Callable[..., None]:
        def fire(*args: Any) -> None:
            if owner in disposed:
                log.append(f"{kind} of fiber {owner} fired after its disposal")

        return fire

    for step in range(60):
        action = rng.choice(
            ["start", "start", "timer", "advance", "advance", "dispose"]
        )
        if action == "start":
            live[next_id] = root.use(idle)
            next_id += 1
        elif action == "timer" and live:
            owner = rng.choice(list(live))
            ctx = live[owner].ctx
            kind = rng.choice(
                ["timeout", "interval", "throttle", "debounce", "sleep", "ticks"]
            )
            delay = rng.choice([0.25, 0.5, 1.0])
            callback = watch(owner, kind)
            if kind == "sleep":
                on_resolve(ctx[kind](delay), callback)
            elif kind == "ticks":
                on_resolve(ctx[kind](delay).__anext__(), callback)
            elif kind in ("timeout", "interval"):
                ctx[kind](callback, delay)
            else:
                ctx[kind](callback, delay)()
        elif action == "advance":
            clock.advance(rng.choice([0.0, 0.25, 0.5, 1.5]))
        elif action == "dispose" and live:
            owner = rng.choice(list(live))
            live.pop(owner).dispose()
            disposed.add(owner)
        assert not log, f"seed {seed}, step {step}: {log}"
    clock.advance(10.0)
    assert not log, f"seed {seed}: {log}"
