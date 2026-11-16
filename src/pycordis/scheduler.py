"""Schedulers and tasks.

A scheduler runs the work of one root context on its owner thread. A task is a
coroutine that a scheduler drives step by step: it runs until it awaits a `Deferred`,
parks, and resumes in a later step when the `Deferred` completes. pycordis does not
use an event loop.

Start with `ImmediateScheduler` for tests and headless programs, or with
`FramePumpScheduler` for a host that has a frame loop. Use `spawn` to run a coroutine on
a scheduler in a later step, and `start` to run its first step at once.
"""

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from functools import wraps
from operator import attrgetter
from typing import Any

from pycordis.deferred import Deferred
from pycordis.errors import CordisError


class Scheduler(ABC):
    """Runs work on the owner thread, one step at a time.

    A host selects the scheduler when it creates the root context. A subclass
    implements `submit`; the base class keeps the parked tasks and the owner thread.

    The thread that creates the scheduler is the owner thread. Every method of the
    context, its fibers, and its services runs on that thread. Only `submit` and the
    completion of a `Deferred` are allowed from other threads.
    """

    def __init__(self) -> None:
        self._parked: set[_Task[Any]] = set()
        self._owner = threading.get_ident()

    def assert_owner(self) -> None:
        """Check that the calling thread is the owner thread.

        Raises:
            CordisError: With the message `WRONG_THREAD` when another thread calls.
        """
        if threading.get_ident() != self._owner:
            raise CordisError("WRONG_THREAD")

    @abstractmethod
    def submit(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the owner thread in a later step.

        This is the one operation that any thread can call to hand work to the
        owner thread. `fn` never runs on the calling thread unless that thread is
        the owner thread and the scheduler is idle.
        """

    @contextmanager
    def call(self) -> Iterator[None]:
        """Mark the span of one outermost call into pycordis.

        A scheduler that runs work when a call returns does so when the outermost
        `call` block exits. The default does nothing.
        """
        yield

    def hold(self, task: "_Task[Any]") -> None:
        """Keep `task` alive while it waits for a `Deferred`.

        A suspended coroutine that nothing references runs its `finally` blocks when
        Python collects it. Holding the task prevents that teardown, so a task ends
        only by finishing, failing, or being resumed.
        """
        self._parked.add(task)

    def release(self, task: "_Task[Any]") -> None:
        """Stop holding `task`. It is resuming."""
        self._parked.discard(task)


class ImmediateScheduler(Scheduler):
    """A scheduler that runs ready work when the outermost call returns.

    Submitted work runs at once when the owner thread is idle, so a top-level call
    returns after all work that it started has run. Work that is submitted while
    other work runs waits in a queue and runs in the same drain. Work that other
    threads submit runs at the next drain on the owner thread.

    Examples:
        ```python
        >>> scheduler = ImmediateScheduler()
        >>> async def work() -> int:
        ...     await Deferred.resolved(None)
        ...     return 7
        >>> spawn(scheduler, work()).result()
        7
        ```
    """

    def __init__(self) -> None:
        super().__init__()
        self._ready = threading.Condition()
        self._queue: deque[Callable[[], None]] = deque()
        self._depth = 0
        self._draining = False

    def submit(self, fn: Callable[[], None]) -> None:
        with self._ready:
            self._queue.append(fn)
            self._ready.notify_all()
        if self._depth == 0 and threading.get_ident() == self._owner:
            self._drain()

    @contextmanager
    def call(self) -> Iterator[None]:
        """Defer draining until the outermost `call` block exits."""
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if self._depth == 0:
                self._drain()

    def run_until_quiet(self, timeout: float | None = None) -> None:
        """Run work until no task is ready and no task waits for a `Deferred`.

        Unlike an ordinary drain, this blocks while a parked task waits for a
        `Deferred` that another thread will complete. A headless program calls it
        to finish deferred teardown before it exits. Call it on the owner thread.

        Args:
            timeout: The longest wait, in seconds. `None` waits without limit.

        Raises:
            TimeoutError: If a task is still parked after `timeout` seconds.
            CordisError: If another thread calls (`WRONG_THREAD`).
        """
        self.assert_owner()
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            self._drain()
            with self._ready:
                if self._queue:
                    continue
                if not self._parked:
                    return
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(
                        f"{len(self._parked)} task(s) still wait for a Deferred"
                    )
                self._ready.wait(remaining)

    def _drain(self) -> None:
        if self._draining:
            return
        self._draining = True
        try:
            while True:
                with self._ready:
                    if not self._queue:
                        return
                    fn = self._queue.popleft()
                fn()
        finally:
            self._draining = False


class FramePumpScheduler(Scheduler):
    """A scheduler that a host with a frame loop drives with `pump`.

    Submitted work waits in a queue. `pump` runs a bounded number of steps and never
    blocks, so a host calls it once per frame without a long pause. Work that any
    thread submits runs on the owner thread in a later `pump` call.

    To run all ready work, for example before the first frame, call `pump` with a limit
    that is larger than the work. A task that waits for a `Deferred` stays parked, and
    it resumes in the `pump` call that follows the completion.

    Examples:
        ```python
        >>> scheduler = FramePumpScheduler()
        >>> scheduler.submit(lambda: print("step"))
        >>> scheduler.pump(10)
        step
        1
        >>> scheduler.pump(10)
        0
        ```
    """

    def __init__(self) -> None:
        super().__init__()
        self._queue: deque[Callable[[], None]] = deque()

    def submit(self, fn: Callable[[], None]) -> None:
        self._queue.append(fn)

    def pump(self, max_steps: int) -> int:
        """Run at most `max_steps` queued steps, then return.

        A step that another step submits is in the same call when the limit allows it.
        An empty queue returns `0` at once. If a step raises, the exception goes to the
        caller and the later steps stay queued. Call it on the owner thread.

        Args:
            max_steps: The most steps to run. `0` runs none.

        Returns:
            The number of steps that ran.

        Raises:
            ValueError: If `max_steps` is negative.
            CordisError: If another thread calls (`WRONG_THREAD`).
        """
        self.assert_owner()
        if max_steps < 0:
            raise ValueError(f"max_steps must not be negative, got {max_steps}")
        queue = self._queue
        steps = 0
        while steps < max_steps and queue:
            steps += 1
            queue.popleft()()
        return steps


def owner_thread[**P, R](attribute: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Make a method check that it runs on the owner thread of its scheduler.

    Args:
        attribute: The name of the instance attribute that holds the scheduler.

    Returns:
        A decorator. The decorated method raises `CordisError("WRONG_THREAD")` when
        another thread calls it.
    """
    get_scheduler = attrgetter(attribute)

    def decorate(method: Callable[P, R]) -> Callable[P, R]:
        @wraps(method)
        def checked(*args: P.args, **kwargs: P.kwargs) -> R:
            get_scheduler(args[0]).assert_owner()
            return method(*args, **kwargs)

        return checked

    return decorate


class _Task[T]:
    """A coroutine that runs on a scheduler and completes a `Deferred`."""

    def __init__(
        self,
        scheduler: Scheduler,
        coroutine: Coroutine[Any, Any, T],
        result: Deferred[T],
    ) -> None:
        self._scheduler = scheduler
        self._coroutine = coroutine
        self._result = result

    def run(self) -> None:
        """Run the coroutine until it finishes or awaits an incomplete `Deferred`."""
        error: BaseException | None = None
        while True:
            try:
                if error is None:
                    yielded = self._coroutine.send(None)
                else:
                    yielded = self._coroutine.throw(error)
            except StopIteration as stop:
                self._result.resolve(stop.value)
                return
            except Exception as failure:
                self._result.reject(failure)
                return
            if isinstance(yielded, Deferred):
                break
            error = TypeError(
                "a task can await only a Deferred or an object whose __await__ "
                f"forwards to one, not {yielded!r}"
            )
        self._scheduler.hold(self)
        yielded.add_done_callback(lambda _: self._scheduler.submit(self._resume))

    def _resume(self) -> None:
        self._scheduler.release(self)
        self.run()


def spawn[T](scheduler: Scheduler, coroutine: Coroutine[Any, Any, T]) -> Deferred[T]:
    """Run `coroutine` as a task on `scheduler`.

    The first step is submitted to the scheduler, so a scheduler that is busy runs it
    after the current step. The coroutine awaits only a `Deferred`, or an object that
    forwards its `await` to one.

    Args:
        scheduler: The scheduler that drives the task.
        coroutine: The coroutine to run.

    Returns:
        A `Deferred` that completes with the coroutine's return value, or rejects with
        the exception that ends it.
    """
    result = Deferred[T]()
    scheduler.submit(_Task(scheduler, coroutine, result).run)
    return result


def start[T](scheduler: Scheduler, coroutine: Coroutine[Any, Any, T]) -> Deferred[T]:
    """Run the first step of `coroutine` now, and the other steps on `scheduler`.

    Call it on the owner thread inside `Scheduler.call`, so that the scheduler does
    not run a later step before this call returns. The coroutine awaits only a
    `Deferred`, or an object that forwards its `await` to one.

    Args:
        scheduler: The scheduler that drives the later steps.
        coroutine: The coroutine to run.

    Returns:
        A `Deferred` that completes with the coroutine's return value, or rejects with
        the exception that ends it. It is already complete when the coroutine ends
        without an `await`.
    """
    result = Deferred[T]()
    _Task(scheduler, coroutine, result).run()
    return result
