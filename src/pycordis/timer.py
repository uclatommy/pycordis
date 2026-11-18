"""Timers that belong to the plugin that starts them.

A timer that a plugin starts and does not stop runs after the plugin unloads. The timer
service avoids this: each timer is an effect of the fiber that starts it, so the fiber
reverts the timer when it unloads (see the user guide). The service reads the time from
a `Clock`, which the host provides as the service `clock`. A change of the clock reloads
the timer service and every plugin that injects `timer`, so no timer runs on an old
clock.

Start with `TimerService`, and give the host a clock: `ManualClock` for tests and for a
host with a frame loop, or `ThreadingClock` for a headless host. This module is not
imported by `pycordis`: import it when you need it.

Examples:
    ```python
    >>> clock = ManualClock()
    >>> ctx.provide(CLOCK, clock)
    >>> ctx.use(TimerService)
    >>> def plugin(ctx, config):
    ...     ctx["timeout"](lambda: print("late"), 1.0)
    >>> plugin.inject = ["timer"]
    >>> fiber = ctx.use(plugin)
    >>> clock.advance(1.0)
    late
    ```
"""

import heapq
import itertools
import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Protocol, Self

from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.keys import Key
from pycordis.scheduler import Scheduler
from pycordis.service import Service

if TYPE_CHECKING:
    from pycordis.context import Context
    from pycordis.fiber import EffectHandle, Fiber

__all__ = [
    "CLOCK",
    "Clock",
    "ManualClock",
    "Scheduled",
    "ThreadingClock",
    "Ticks",
    "TimerService",
]

logger = logging.getLogger(__name__)

type Cancel = Callable[[], None]


class Clock(Protocol):
    """The source of time for the timer service.

    A host provides a clock as the service `clock` (see `CLOCK`). `ManualClock` and
    `ThreadingClock` implement it.

    A clock runs each callback on the owner thread of the root context, and a call that
    was cancelled never runs.
    """

    def now(self) -> float:
        """Return the current time in seconds, on a monotonic scale."""
        ...

    def call_later(self, delay: float, callback: Callable[[], None]) -> Cancel:
        """Run `callback` once, `delay` seconds from now.

        Args:
            delay: The wait in seconds. A value of `0` or less means "due now".
            callback: The function to run.

        Returns:
            A function that cancels the call. A call that is already cancelled, or
            already run, ignores the cancel.
        """
        ...


CLOCK: Key[Clock] = Key("clock")
"""The key of the service that gives the `Clock` to the timer service."""


class _Call:
    """One scheduled call. The clock that owns it and its cancel function share it."""

    __slots__ = ("callback", "cancelled")

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.cancelled = False

    def run(self) -> None:
        if not self.cancelled:
            self.callback()

    def cancel(self) -> None:
        self.cancelled = True


class ManualClock:
    """A clock that moves only when the host calls `advance`.

    Use it in tests, and in a host with a frame loop: call `advance` each frame with the
    elapsed time. All calls run on the thread that calls `advance`, which must be the
    owner thread of the root context.

    Examples:
        ```python
        >>> clock = ManualClock()
        >>> cancel = clock.call_later(2.0, lambda: print("due"))
        >>> clock.advance(1.0)
        >>> clock.advance(1.0)
        due
        >>> clock.now()
        2.0
        ```
    """

    def __init__(self, start: float = 0.0) -> None:
        """Create a clock.

        Args:
            start: The first value of `now()`.
        """
        self._now = start
        self._heap: list[tuple[float, int, _Call]] = []
        self._order = itertools.count()

    def now(self) -> float:
        """Return the current time. In a callback, it is the deadline of the call."""
        return self._now

    def call_later(self, delay: float, callback: Callable[[], None]) -> Cancel:
        """Run `callback` when `advance` reaches `now() + delay`. See `Clock`."""
        call = _Call(callback)
        deadline = self._now + max(delay, 0.0)
        heapq.heappush(self._heap, (deadline, next(self._order), call))
        return call.cancel

    def advance(self, seconds: float) -> None:
        """Move the time forward, and run each call that becomes due.

        The calls run in the order of their deadlines, and calls with the same deadline
        run in the order of `call_later`. A callback can call `call_later`: a new call
        that is due inside the step runs in the same `advance`. At the end, `now()` is
        the time at the start plus `seconds`.

        Args:
            seconds: The step. `0` runs the calls that are due now.

        Raises:
            ValueError: If `seconds` is negative.
        """
        if not seconds >= 0:
            raise ValueError(f"seconds must not be negative, got {seconds}")
        target = self._now + seconds
        heap = self._heap
        while heap and heap[0][0] <= target:
            deadline, _, call = heapq.heappop(heap)
            self._now = max(self._now, deadline)
            call.run()
        self._now = target


class ThreadingClock:
    """A clock for a headless host. A worker thread waits, and the scheduler runs.

    One daemon thread keeps the deadlines. When a call is due, the thread hands it to
    `Scheduler.submit`, so the callback runs on the owner thread of the root context. A
    cancel on the owner thread stops a call that the thread has already submitted.

    `ImmediateScheduler` runs work that another thread submits only when the owner
    thread drains. A headless host that waits for timer callbacks calls
    `ImmediateScheduler.serve` on the owner thread, with a stop condition.

    Call `close` after the root context is disposed. The disposal of the root already
    cancels every timer of the timer service.

    Examples:
        ```python
        >>> scheduler = ImmediateScheduler()
        >>> clock = ThreadingClock(scheduler)
        >>> clock.call_later(0.5, lambda: print("due"))
        >>> clock.close()
        ```
    """

    def __init__(self, scheduler: Scheduler) -> None:
        """Create the clock and start its worker thread.

        Args:
            scheduler: The scheduler that runs the callbacks.
        """
        self._scheduler = scheduler
        self._ready = threading.Condition()
        self._heap: list[tuple[float, int, _Call]] = []
        self._order = itertools.count()
        self._closed = False
        self._worker = threading.Thread(
            target=self._work, name="pycordis-clock", daemon=True
        )
        self._worker.start()

    def now(self) -> float:
        """Return `time.monotonic()`."""
        return time.monotonic()

    def call_later(self, delay: float, callback: Callable[[], None]) -> Cancel:
        """Run `callback` on the owner thread `delay` seconds from now. See `Clock`.

        Raises:
            CordisError: If the clock is closed.
        """
        call = _Call(callback)
        deadline = time.monotonic() + max(delay, 0.0)
        with self._ready:
            if self._closed:
                raise CordisError("clock is closed")
            heapq.heappush(self._heap, (deadline, next(self._order), call))
            self._ready.notify_all()
        return call.cancel

    def close(self, timeout: float | None = None) -> None:
        """Drop the pending calls, stop the worker thread, and wait for it to end.

        A second call does nothing.

        Args:
            timeout: The longest wait in seconds. `None` waits without limit.

        Raises:
            TimeoutError: If the worker thread is still alive after `timeout` seconds.
        """
        with self._ready:
            self._closed = True
            self._heap.clear()
            self._ready.notify_all()
        self._worker.join(timeout)
        if self._worker.is_alive():
            raise TimeoutError("the clock thread did not stop")

    def _work(self) -> None:
        while (call := self._next_due()) is not None:
            try:
                self._scheduler.submit(call.run)
            except Exception:
                logger.exception("the scheduler did not accept a timer call")

    def _next_due(self) -> _Call | None:
        """Wait for the earliest call to be due and remove it. `None` after `close`."""
        with self._ready:
            while not self._closed:
                if not self._heap:
                    self._ready.wait()
                    continue
                wait = self._heap[0][0] - time.monotonic()
                if wait <= 0:
                    return heapq.heappop(self._heap)[2]
                self._ready.wait(wait)
        return None


def _check_delay(delay: float, *, positive: bool = False) -> None:
    """Raise `ValueError` unless `delay` is a valid wait in seconds."""
    if not delay >= 0 or (positive and delay == 0):
        kind = "positive" if positive else "not negative"
        raise ValueError(f"delay must be {kind}, got {delay}")


def _run_logged(fiber: "Fiber", label: str, call: Callable[[], object]) -> None:
    """Run `call`, and log an `Exception` with the logger of `fiber`.

    A failed callback of one timer does not stop the other timers (`EFF-ISOLATE`).
    """
    fiber.ctx.scheduler._claim_step(fiber, label)
    try:
        call()
    except Exception as error:
        fiber.logger.error("error in %s callback", label, exc_info=error)


class Scheduled[**P]:
    """A function that the timer service calls later, and that a fiber disposes.

    `TimerService.throttle` and `TimerService.debounce` return it. A call of the object
    cancels the pending call of the earlier call and then schedules its own call. After
    `dispose`, or after the fiber unloads, a call does nothing.

    Examples:
        ```python
        >>> save = ctx["debounce"](lambda text: print("save", text), 0.5)
        >>> save("a")
        >>> save("ab")
        >>> clock.advance(0.5)
        save ab
        ```
    """

    def __init__(
        self, ctx: "Context", label: str, trigger: Callable[P, Cancel | None]
    ) -> None:
        self._trigger = trigger
        self._cancel: Cancel | None = None
        self._disposed = False
        self._handle = ctx.effect(lambda: self._close, label)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None:
        """Cancel the pending call, and schedule the call for these arguments."""
        if self._disposed:
            return
        self._clear()
        self._cancel = self._trigger(*args, **kwargs)

    def dispose(self) -> "Deferred[None] | None":
        """Cancel the pending call and stop the object. It is the effect handle.

        Returns:
            What the effect handle returns: `None`, because the revert ends at once.
        """
        return self._handle()

    def _close(self) -> None:
        self._disposed = True
        self._clear()

    def _clear(self) -> None:
        cancel, self._cancel = self._cancel, None
        if cancel is not None:
            cancel()


class Ticks:
    """An async iterator that gives one item for each tick of a timer.

    `TimerService.ticks` returns it. A task reads it with `async for`, or with
    `await ticks.__anext__()`. Each read waits for one tick, and a tick resolves the
    oldest pending read. A tick that comes while no read is pending is lost.

    The iterator ends in one of three ways, and the first one stays:

    - `aclose` ends it normally: every pending and later read raises
      `StopAsyncIteration`.
    - `athrow` ends it with an error: every pending and later read raises that error.
    - The fiber that created it unloads: every pending and later read raises
      `CordisError("Context has been disposed")`.

    An exit from `async for` with `break` or with an exception does not close the
    iterator. Use `contextlib.aclosing` to stop the timer at the exit.

    Examples:
        ```python
        >>> async def watch(ctx):
        ...     async with aclosing(ctx["ticks"](1.0)) as ticks:
        ...         async for _ in ticks:
        ...             print("tick")
        ...             break
        ```
    """

    def __init__(
        self,
        every: Callable[[Callable[[], None], Callable[[], None]], "EffectHandle"],
    ) -> None:
        self._pending: deque[Deferred[None]] = deque()
        self._end: BaseException | None = None
        self._handle = every(self._tick, self._unload)

    def __aiter__(self) -> Self:
        return self

    def __anext__(self) -> Deferred[None]:
        """Return a `Deferred` that completes at the next tick.

        Returns:
            A `Deferred` that resolves at a tick. After the iterator has ended, it is
            already rejected with the error of the end.
        """
        read = Deferred[None]()
        if self._end is not None:
            read.reject(self._end)
        else:
            self._pending.append(read)
        return read

    def aclose(self) -> Deferred[None]:
        """End the iterator, and stop the timer.

        Every pending read and every later read raises `StopAsyncIteration`. If the
        iterator has already ended, its end does not change.

        Returns:
            A `Deferred` that is already resolved.
        """
        self._finish(StopAsyncIteration())
        return Deferred.resolved(None)

    def athrow(self, error: BaseException) -> Deferred[None]:
        """End the iterator with `error`, and stop the timer.

        Every pending read and every later read raises `error`. If the iterator has
        already ended, its end does not change.

        Args:
            error: The error for the reads.

        Returns:
            A `Deferred` that is already rejected with `StopAsyncIteration`.
        """
        self._finish(error)
        result = Deferred[None]()
        result.reject(StopAsyncIteration())
        return result

    def _tick(self) -> None:
        if self._pending:
            self._pending.popleft().resolve(None)

    def _unload(self) -> None:
        """End the iterator when its effect reverts without a terminal state."""
        self._end_reads(CordisError("Context has been disposed"))

    def _finish(self, error: BaseException) -> None:
        self._end_reads(error)
        self._handle()

    def _end_reads(self, error: BaseException) -> None:
        if self._end is not None:
            return
        self._end = error
        pending, self._pending = self._pending, deque()
        for read in pending:
            read.reject(error)


class TimerService(Service):
    """The timers of a plugin, as effects of its fiber.

    The service has the name `timer` and injects the service `clock` (see `CLOCK`). A
    plugin that injects `timer` reads the timer methods as `ctx["timeout"]` and so on.
    Each timer is an effect of the plugin's fiber with a label such as `ctx.timeout()`,
    so `Fiber.get_effects` lists it, and the fiber stops it when the fiber unloads or
    when the plugin calls the handle.

    Delays are in seconds. A negative delay raises `ValueError`. An `Exception` from a
    callback that the clock runs is logged with the logger of the plugin's fiber, and
    the other timers continue.

    Examples:
        ```python
        >>> ctx.provide(CLOCK, ManualClock())
        >>> ctx.use(TimerService)
        >>> def plugin(ctx, config):
        ...     ctx["interval"](lambda: print("tick"), 1.0)
        >>> plugin.inject = ["timer"]
        >>> fiber = ctx.use(plugin)
        ```
    """

    provide = "timer"
    inject = ["clock"]

    def __init__(self, ctx: "Context", config: object = None) -> None:
        clock = ctx[CLOCK]
        super().__init__(ctx)
        self._clock: Clock = clock
        ctx.mixin(
            self.name,
            ("timeout", "sleep", "interval", "ticks", "throttle", "debounce"),
        )

    def timeout(self, callback: Callable[[], object], delay: float) -> "EffectHandle":
        """Call `callback` once, `delay` seconds from now.

        The timer reverts its own effect before it calls `callback`, so a fired timer is
        not in `Fiber.get_effects`, and a call of the handle then does nothing.

        Args:
            callback: The function to call.
            delay: The wait in seconds. `0` means "due now".

        Returns:
            The handle of the effect. Call it to cancel the timer.

        Raises:
            ValueError: If `delay` is negative.
        """
        _check_delay(delay)
        fiber = self.ctx._fiber

        def fire() -> None:
            handle()
            _run_logged(fiber, "ctx.timeout()", callback)

        handle = self.ctx.effect(
            lambda: self._clock.call_later(delay, fire), "ctx.timeout()"
        )
        return handle

    def sleep(self, delay: float) -> Deferred[None]:
        """Return a `Deferred` that resolves `delay` seconds from now.

        The wait is an effect of the plugin's fiber with the label `ctx.timeout()`. The
        effect ends when the `Deferred` resolves. If the fiber unloads first, the
        `Deferred` rejects with `CordisError("Context has been disposed")`.

        Args:
            delay: The wait in seconds. `0` means "due now".

        Returns:
            A `Deferred` for a task to await.

        Raises:
            ValueError: If `delay` is negative.

        Examples:
            ```python
            >>> async def pause(ctx):
            ...     await ctx["sleep"](1.0)
            ```
        """
        _check_delay(delay)
        waiting = Deferred[None]()

        def fire() -> None:
            waiting.resolve(None)
            handle()

        def start() -> Cancel:
            cancel = self._clock.call_later(delay, fire)

            def revert() -> None:
                cancel()
                if not waiting.done():
                    waiting.reject(CordisError("Context has been disposed"))

            return revert

        handle = self.ctx.effect(start, "ctx.timeout()")
        return waiting

    def interval(self, callback: Callable[[], object], delay: float) -> "EffectHandle":
        """Call `callback` every `delay` seconds until the handle or the fiber stops it.

        Each tick is due `delay` seconds after the previous tick runs. When the host
        is late, `callback` runs once, and the next call is one `delay` later.

        Args:
            callback: The function to call.
            delay: The time between two calls in seconds.

        Returns:
            The handle of the effect. Call it to stop the timer.

        Raises:
            ValueError: If `delay` is not positive.
        """
        _check_delay(delay, positive=True)
        fiber = self.ctx._fiber
        return self._every(
            delay,
            lambda: _run_logged(fiber, "ctx.interval()", callback),
            "ctx.interval()",
        )

    def ticks(self, delay: float) -> Ticks:
        """Return an async iterator that gives one item every `delay` seconds.

        The timer is an effect of the plugin's fiber with the label `ctx.interval()`.
        Each tick is due `delay` seconds after the previous tick runs, so a late host
        gets one tick, not one for each missed period.

        Args:
            delay: The time between two ticks in seconds.

        Returns:
            The `Ticks` iterator. Close it with `Ticks.aclose` to stop the timer.

        Raises:
            ValueError: If `delay` is not positive.
        """
        _check_delay(delay, positive=True)
        return Ticks(
            lambda tick, unload: self._every(delay, tick, "ctx.interval()", unload)
        )

    def throttle[**P](
        self, callback: Callable[P, object], delay: float, trailing: bool = True
    ) -> Scheduled[P]:
        """Limit `callback` to one call in each `delay` seconds.

        The first call of the result runs `callback` at once, inside the caller's call,
        so an error goes to the caller. A call that comes less than `delay` after the
        last run of `callback` waits until `delay` has passed since that run, and then
        runs with the arguments of the newest call (the trailing call). A later call
        replaces a waiting trailing call.

        Args:
            callback: The function to limit.
            delay: The shortest time between two runs in seconds.
            trailing: With `False`, a call that comes too early is dropped.

        Returns:
            The function to call in place of `callback`.

        Raises:
            ValueError: If `delay` is negative.
        """
        _check_delay(delay)
        fiber = self.ctx._fiber
        clock = self._clock
        last = -math.inf

        def execute(*args: P.args, **kwargs: P.kwargs) -> None:
            nonlocal last
            last = clock.now()
            callback(*args, **kwargs)

        def trigger(*args: P.args, **kwargs: P.kwargs) -> Cancel | None:
            remaining = delay - clock.now() + last
            if remaining <= 0:
                execute(*args, **kwargs)
                return None
            if not trailing:
                return None
            call = partial(execute, *args, **kwargs)
            return clock.call_later(
                remaining, lambda: _run_logged(fiber, "ctx.throttle()", call)
            )

        return Scheduled(self.ctx, "ctx.throttle()", trigger)

    def debounce[**P](
        self, callback: Callable[P, object], delay: float
    ) -> Scheduled[P]:
        """Call `callback` after `delay` seconds with no call of the result.

        Each call of the result restarts the wait, and the callback runs with the
        arguments of the newest call.

        Args:
            callback: The function to call.
            delay: The quiet time in seconds.

        Returns:
            The function to call in place of `callback`.

        Raises:
            ValueError: If `delay` is negative.
        """
        _check_delay(delay)
        fiber = self.ctx._fiber
        clock = self._clock

        def trigger(*args: P.args, **kwargs: P.kwargs) -> Cancel:
            call = partial(callback, *args, **kwargs)
            return clock.call_later(
                delay, lambda: _run_logged(fiber, "ctx.debounce()", call)
            )

        return Scheduled(self.ctx, "ctx.debounce()", trigger)

    def _every(
        self,
        delay: float,
        tick: Callable[[], None],
        label: str,
        unload: Callable[[], None] = lambda: None,
    ) -> "EffectHandle":
        """Register an effect that calls `tick` every `delay` seconds.

        The effect schedules each tick `delay` seconds after the previous tick runs, so
        a stalled host gets one tick after the stall, not one for each missed period.
        It schedules the next tick before it runs `tick`, so a `tick` that
        reverts the effect also cancels the next tick. The revert calls `unload`
        after it cancels the timer.
        """
        clock = self._clock

        def start() -> Cancel:
            def fire() -> None:
                nonlocal cancel
                cancel = clock.call_later(delay, fire)
                tick()

            cancel = clock.call_later(delay, fire)

            def stop() -> None:
                cancel()
                unload()

            return stop

        return self.ctx.effect(start, label)
