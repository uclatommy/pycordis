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

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.scheduler import ImmediateScheduler, spawn

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
