"""Shared pytest configuration for the pycordis test suite.

The suite reports each test whose `call` phase takes `SLOW_THRESHOLD_S` seconds or more
and that has no `@pytest.mark.slow` marker. The report is the "unmarked slow tests"
section of the terminal summary. Only a run of the full suite is authoritative: in a
subset, a one-time import cost goes to the first test that runs.
"""

import heapq
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from loader_fakes import RecordingLoader

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.scheduler import ImmediateScheduler, spawn
from pycordis.traceable import Tracker

SLOW_THRESHOLD_S = 0.1

_unmarked_slow: list[tuple[str, float]] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record a `call` phase at or above the threshold that has no `slow` marker."""
    if report.when != "call" or report.duration < SLOW_THRESHOLD_S:
        return
    if "slow" in report.keywords:
        return
    _unmarked_slow.append((report.nodeid, report.duration))


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Write the unmarked slow tests, slowest first."""
    if not _unmarked_slow:
        return
    terminalreporter.section("unmarked slow tests")
    terminalreporter.write_line(
        f"Mark these with @pytest.mark.slow (call time >= {SLOW_THRESHOLD_S}s):"
    )
    for nodeid, duration in sorted(_unmarked_slow, key=lambda item: -item[1]):
        terminalreporter.write_line(f"{duration:.3f}s {nodeid}")


class FakeClock:
    """A clock that moves only when a test calls `advance`.

    `sleep` returns a `Deferred` that completes when the clock reaches its deadline. The
    scheduler drains after each completion, so a task that a timer wakes runs before the
    next timer fires.
    """

    def __init__(self) -> None:
        self.now = 0
        self._timers: list[tuple[int, int, Deferred[None]]] = []
        self._count = 0

    def sleep(self, ms: int) -> Deferred[None]:
        """Return a `Deferred` that completes `ms` milliseconds from now."""
        deferred = Deferred[None]()
        self._count += 1
        heapq.heappush(self._timers, (self.now + ms, self._count, deferred))
        return deferred

    def advance(self, ms: int) -> None:
        """Move the clock forward and complete each due timer in deadline order."""
        target = self.now + ms
        while self._timers and self._timers[0][0] <= target:
            deadline, _, deferred = heapq.heappop(self._timers)
            self.now = deadline
            deferred.resolve(None)
        self.now = target


event = "custom-event"
"""The name of the event that the ported cases dispatch."""


class Symbol:
    """A private event name. `str()` gives `Symbol(<description>)`."""

    def __init__(self, description: str) -> None:
        self.description = description

    def __str__(self) -> str:
        return f"Symbol({self.description})"


class Recorder:
    """A listener that records its calls and runs `impl`, if there is one."""

    def __init__(self, impl: Callable[..., Any] | None = None) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.impl = impl

    def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return self.impl(*args) if self.impl is not None else None


class Session:
    """A dispatch `this` object that filters by the `filter` meta value of a context."""

    def __init__(self, flag: bool) -> None:
        self.flag = flag

    def cordis_filter(self, ctx: Context) -> bool:
        accept = ctx.meta.get("filter")
        return True if accept is None else bool(accept(self))


class Filter:
    """A context filter that accepts the sessions that have the same flag."""

    def __init__(self, flag: bool) -> None:
        self.flag = flag

    def filter(self, session: Session) -> bool:
        return session.flag == self.flag


class Counter:
    """A traceable object: `increase` registers an effect for its use site."""

    cordis_tracker = Tracker(property="ctx", associate="counter")

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.value = 0

    def increase(self) -> Any:
        def effect() -> Callable[[], None]:
            self.value += 1
            return self.decrease

        return self.ctx.effect(effect)

    def decrease(self) -> None:
        self.value -= 1


def hook_snapshot(ctx: Context) -> dict[str, int]:
    """Return the number of listeners of each event of `ctx`, by event name."""
    return {str(name): count for name, count in ctx.events.listener_counts().items()}


@pytest.fixture
def scheduler() -> ImmediateScheduler:
    """A scheduler that drains when the outermost call returns."""
    return ImmediateScheduler()


@pytest.fixture
def root(scheduler: ImmediateScheduler) -> Context:
    """A root context on `scheduler`."""
    return Context(scheduler)


@pytest.fixture
def clock() -> FakeClock:
    """A clock that a test advances by hand."""
    return FakeClock()


@pytest.fixture
def run(
    scheduler: ImmediateScheduler,
) -> Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]:
    """Run a coroutine as a task on `scheduler`, and return its `Deferred`.

    The task runs to quiescence before `run` returns. A task that still waits for a
    timer leaves its `Deferred` incomplete until the test advances the clock.
    """
    return lambda coroutine: spawn(scheduler, coroutine)


@pytest.fixture
def loader(root: Context) -> RecordingLoader:
    """A `RecordingLoader` that runs in `root`."""
    root.use(RecordingLoader)
    return root.get("loader")  # type: ignore[return-value]
