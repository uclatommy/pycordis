"""Shared pytest configuration for the pycordis test suite.

The suite reports each test whose `call` phase takes `SLOW_THRESHOLD_S` seconds or more
and that has no `@pytest.mark.slow` marker. The report is the "unmarked slow tests"
section of the terminal summary. Only a run of the full suite is authoritative: in a
subset, a one-time import cost goes to the first test that runs.
"""

import pytest

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
