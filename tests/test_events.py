"""Ported upstream event cases that need only the root context."""

from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from conftest import FakeClock, Filter, Recorder, Session, Symbol, event, hook_snapshot

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.events import EventOptions

Run = Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]
MODES = ("emit", "parallel", "serial", "bail")


def dispatch(root: Context, mode: str, name: object) -> None:
    """Dispatch `name` in `mode` and wait for an asynchronous mode to finish."""
    result = getattr(root, mode)(name)
    if isinstance(result, Deferred):
        result.result()


@pytest.mark.upstream(
    "events.spec.ts::Events::supports symbol event names across dispatch modes"
)
def test_private_event_names_work_in_every_dispatch_mode(root: Context) -> None:
    name = Symbol("event")
    callback = Recorder(lambda value: value)
    dispose = root.on(name, callback)

    root.emit(name, 1)
    assert root.bail(name, 2) == 2
    assert root.serial(name, 3).result() == 3
    root.parallel(name, 4).result()
    assert [call[0] for call in callback.calls] == [1, 2, 3, 4]

    dispose()
    root.emit(name, 5)
    assert len(callback.calls) == 4


@pytest.mark.upstream(
    "events.spec.ts::Events::supports symbol event names with ctx.once()"
)
def test_once_works_with_a_private_event_name(root: Context) -> None:
    name = Symbol("once")
    callback = Recorder()
    root.once(name, callback)

    root.emit(name)
    root.emit(name)

    assert len(callback.calls) == 1


@pytest.mark.upstream(
    "events.spec.ts::Events::treats prototype property names as ordinary events"
)
@pytest.mark.parametrize("name", ["__proto__", "toString", "constructor"])
def test_names_of_object_attributes_are_ordinary_events(
    root: Context, name: str
) -> None:
    root.emit("hasattr")
    callback = Recorder()
    dispose = root.on(name, callback)

    root.emit(name)
    assert len(callback.calls) == 1
    dispose()
    root.emit(name)

    assert len(callback.calls) == 1
    assert name not in hook_snapshot(root)


@pytest.mark.upstream(
    "events.spec.ts::Events::removes empty event buckets after disposal"
)
def test_an_event_without_listeners_has_no_bucket(root: Context) -> None:
    name = Symbol("temporary")
    first = root.on(name, lambda: None)
    second = root.on(name, lambda: None)

    first()
    assert hook_snapshot(root) == {"Symbol(temporary)": 1}
    second()

    assert hook_snapshot(root) == {}


@pytest.mark.upstream("events.spec.ts::Events::ctx.on()")
@pytest.mark.upstream("events.spec.ts::Events::ctx.once()")
def test_on_and_once_register_a_removable_listener(root: Context) -> None:
    every, single = Recorder(), Recorder()
    dispose_every = root.on(event, every)
    dispose_single = root.once(event, single)

    root.emit(event)
    root.emit(event)
    assert (len(every.calls), len(single.calls)) == (2, 1)

    dispose_every()
    dispose_single()
    root.emit(event)
    assert (len(every.calls), len(single.calls)) == (2, 1)


@pytest.mark.upstream(
    "events.spec.ts::Events::ctx.once() runs once when an earlier listener emits"
    " recursively"
)
def test_once_runs_once_when_an_earlier_listener_emits_again(root: Context) -> None:
    nested = False

    def recurse() -> None:
        nonlocal nested
        if nested:
            return
        nested = True
        root.emit(event)

    root.on(event, recurse)
    callback = Recorder()
    root.once(event, callback)

    root.emit(event)

    assert len(callback.calls) == 1


@pytest.mark.upstream(
    "events.spec.ts::Events::ctx.once() runs once across concurrent serial dispatches"
)
def test_once_runs_once_across_concurrent_serial_dispatches(root: Context) -> None:
    barrier = Deferred[None]()
    root.on(event, lambda: barrier)
    callback = Recorder()
    root.once(event, callback)

    first, second = root.serial(event), root.serial(event)
    barrier.resolve(None)

    assert first.done() and second.done()
    assert len(callback.calls) == 1


@pytest.mark.upstream(
    "events.spec.ts::Events::emit skips listeners removed during dispatch"
)
@pytest.mark.upstream(
    "events.spec.ts::Events::parallel skips listeners removed during dispatch"
)
@pytest.mark.upstream(
    "events.spec.ts::Events::serial skips listeners removed during dispatch"
)
@pytest.mark.upstream(
    "events.spec.ts::Events::bail skips listeners removed during dispatch"
)
@pytest.mark.parametrize("mode", MODES)
def test_dispatch_skips_a_listener_removed_during_the_dispatch(
    root: Context, mode: str
) -> None:
    removed, remaining = Recorder(), Recorder()
    root.on(event, lambda: dispose())
    dispose = root.on(event, removed)
    root.on(event, remaining)

    dispatch(root, mode, event)

    assert (len(removed.calls), len(remaining.calls)) == (0, 1)


@pytest.mark.upstream(
    "events.spec.ts::Events::emit defers added listeners until the next dispatch"
)
@pytest.mark.upstream(
    "events.spec.ts::Events::parallel defers added listeners until the next dispatch"
)
@pytest.mark.upstream(
    "events.spec.ts::Events::serial defers added listeners until the next dispatch"
)
@pytest.mark.upstream(
    "events.spec.ts::Events::bail defers added listeners until the next dispatch"
)
@pytest.mark.parametrize("mode", MODES)
def test_dispatch_defers_a_listener_added_during_the_dispatch(
    root: Context, mode: str
) -> None:
    added = Recorder()
    root.on(event, lambda: root.on(event, added) and None)

    dispatch(root, mode, event)
    assert added.calls == []
    dispatch(root, mode, event)

    assert len(added.calls) == 1


def sum_next(value: int, next_: Callable[[], int]) -> int:
    return value + next_()


@pytest.mark.upstream("events.spec.ts::Events::ctx.waterfall()")
def test_waterfall_chains_listeners_until_one_stops(root: Context) -> None:
    first, second = Recorder(sum_next), Recorder(sum_next)
    root.on("test/waterfall", first)
    root.on("test/waterfall", second)

    assert root.waterfall("test/waterfall", 1, lambda: 2) == 4
    assert (len(first.calls), len(second.calls)) == (1, 1)

    stop, after = Recorder(lambda value, next_: value), Recorder(sum_next)
    root.on("test/waterfall", stop)
    root.on("test/waterfall", after)
    assert root.waterfall("test/waterfall", 1, lambda: 2) == 3
    assert (len(first.calls), len(second.calls)) == (2, 2)
    assert (len(stop.calls), len(after.calls)) == (1, 0)


@pytest.mark.upstream(
    "events.spec.ts::Events::waterfall continues past a removed listener"
)
def test_waterfall_continues_past_a_removed_listener(root: Context) -> None:
    def remove_then_next(value: int, next_: Callable[[], int]) -> int:
        dispose()
        return value + next_()

    root.on("test/waterfall", remove_then_next)
    removed, remaining = Recorder(sum_next), Recorder(sum_next)
    dispose = root.on("test/waterfall", removed)
    root.on("test/waterfall", remaining)
    terminal = Recorder(lambda: 2)

    assert root.waterfall("test/waterfall", 1, terminal) == 4
    assert (len(removed.calls), len(remaining.calls), len(terminal.calls)) == (0, 1, 1)


@pytest.mark.upstream(
    "events.spec.ts::Events::waterfall defers added listeners until the next dispatch"
)
def test_waterfall_defers_a_listener_added_during_the_dispatch(root: Context) -> None:
    added = Recorder(sum_next)

    def add(value: int, next_: Callable[[], int]) -> int:
        root.on("test/waterfall", added)
        return next_()

    root.on("test/waterfall", add)

    assert root.waterfall("test/waterfall", 1, lambda: 2) == 2
    assert added.calls == []
    assert root.waterfall("test/waterfall", 1, lambda: 2) == 3
    assert len(added.calls) == 1


@pytest.mark.upstream(
    "events.spec.ts::Events::waterfall continues after a recursive dispatch has"
    " consumed a once listener"
)
def test_waterfall_continues_after_a_nested_dispatch_consumed_a_once_listener(
    root: Context,
) -> None:
    nested = False

    def recurse(value: int, next_: Callable[[], int]) -> int:
        nonlocal nested
        if not nested:
            nested = True
            root.waterfall("test/waterfall", value, lambda: 2)
        return next_()

    root.on("test/waterfall", recurse)
    once = Recorder(sum_next)
    root.once("test/waterfall", once)
    root.on("test/waterfall", sum_next)

    assert root.waterfall("test/waterfall", 1, lambda: 2) == 3
    assert len(once.calls) == 1


@pytest.mark.upstream(
    "events.spec.ts::Events::ctx.waterfall() rejects duplicate next()"
)
def test_waterfall_rejects_a_second_next(root: Context) -> None:
    terminal = Recorder(lambda: 2)

    def twice(value: int, next_: Callable[[], int]) -> int:
        next_()
        return next_()

    callback = Recorder(twice)
    root.on("test/waterfall", callback)

    with pytest.raises(CordisError, match=r"next\(\) called multiple times"):
        root.waterfall("test/waterfall", 1, terminal)
    assert (len(callback.calls), len(terminal.calls)) == (1, 1)


@pytest.mark.upstream(
    "events.spec.ts::Events::ctx.waterfall() rejects continuations from outer frames"
)
def test_waterfall_rejects_the_next_of_an_outer_listener(root: Context) -> None:
    calls: list[str] = []
    outer: list[Callable[[], int]] = []

    def first(value: int, next_: Callable[[], int]) -> int:
        outer.append(next_)
        calls.append("first")
        return next_()

    def second(value: int, next_: Callable[[], int]) -> int:
        calls.append("second")
        return next_()

    def terminal() -> int:
        calls.append("terminal")
        return outer[0]()

    root.on("test/waterfall", first)
    root.on("test/waterfall", second)

    with pytest.raises(CordisError, match=r"next\(\) called multiple times"):
        root.waterfall("test/waterfall", 1, terminal)
    assert calls == ["first", "second", "terminal"]


@pytest.mark.upstream(
    "events.spec.ts::Events::ctx.waterfall() rejects duplicate next() after awaiting"
)
def test_waterfall_rejects_a_second_next_after_an_await(
    root: Context, run: Run
) -> None:
    terminal = Recorder(lambda: Deferred.resolved(2))

    async def listener(value: int, next_: Callable[[], Deferred[int]]) -> int:
        result = await next_()
        with pytest.raises(CordisError, match=r"next\(\) called multiple times"):
            next_()
        return value + result

    async def main() -> int:
        return await root.waterfall("test/async-waterfall", 1, terminal)

    root.on("test/async-waterfall", listener)

    assert run(main()).result() == 3
    assert len(terminal.calls) == 1


@pytest.mark.upstream(
    "events.spec.ts::Events::ctx.waterfall() supports nested async calls"
)
def test_waterfall_supports_a_nested_async_dispatch(root: Context, run: Run) -> None:
    terminal = Recorder(lambda: Deferred.resolved(2))

    async def listener(value: int, next_: Callable[[], Deferred[int]]) -> int:
        result = await next_()
        if value == 1:
            return result + await root.waterfall("test/async-waterfall", 2, terminal)
        return result

    callback = Recorder(listener)

    async def main() -> int:
        return await root.waterfall("test/async-waterfall", 1, terminal)

    root.on("test/async-waterfall", callback)

    assert run(main()).result() == 4
    assert (len(callback.calls), len(terminal.calls)) == (2, 2)


def filtered_dispatch(
    root: Context, mode: str
) -> tuple[Recorder, Callable[[object], None]]:
    """Return a recorder on a `Filter(True)` context, and a function that dispatches."""
    getattr(root, mode)(event)
    callback = Recorder()
    root.extend(filter=Filter(True).filter).on(event, callback)

    def send(this: object | None) -> None:
        result = (
            getattr(root, mode)(event)
            if this is None
            else getattr(root, mode)(event, this=this)
        )
        if isinstance(result, Deferred):
            result.result()

    return callback, send


@pytest.mark.upstream("events.spec.ts::Events::ctx.emit()")
@pytest.mark.upstream("events.spec.ts::Events::ctx.serial()")
@pytest.mark.upstream("events.spec.ts::Events::ctx.bail()")
@pytest.mark.parametrize("mode", ["emit", "serial", "bail"])
def test_filter_selects_listeners_and_listener_errors_reach_the_caller(
    root: Context, mode: str
) -> None:
    callback, send = filtered_dispatch(root, mode)

    send(None)
    assert len(callback.calls) == 1
    send(Session(False))
    assert len(callback.calls) == 1
    send(Session(True))
    assert len(callback.calls) == 2

    def fail() -> None:
        raise ValueError("message")

    callback.impl = fail
    with pytest.raises(ValueError, match="message"):
        send(None)


@pytest.mark.upstream("events.spec.ts::Events::ctx.parallel()")
def test_parallel_filters_listeners_and_collects_every_error(
    root: Context, clock: FakeClock
) -> None:
    callback, send = filtered_dispatch(root, "parallel")

    send(None)
    send(Session(False))
    assert len(callback.calls) == 1
    send(Session(True))
    assert len(callback.calls) == 2

    settled: list[bool] = []

    async def late() -> None:
        await clock.sleep(0)
        settled.append(True)
        raise ValueError("async")

    dispose = root.on(event, late)

    def fail() -> None:
        raise ValueError("test")

    callback.impl = fail
    result = root.parallel(event)
    assert not result.done()
    clock.advance(0)

    with pytest.raises(ExceptionGroup) as caught:
        result.result()
    assert (
        caught.value.message == 'one or more listeners of event "custom-event" failed'
    )
    assert sorted(str(error) for error in caught.value.exceptions) == ["async", "test"]
    assert settled == [True]
    dispose()


@pytest.mark.upstream("events.spec.ts::Events::ctx.parallel() labels symbol events")
def test_parallel_error_message_names_a_private_event(root: Context) -> None:
    name = Symbol("parallel")

    def fail() -> None:
        raise ValueError("test")

    root.on(name, fail)

    with pytest.raises(ExceptionGroup) as caught:
        root.parallel(name).result()
    assert (
        caught.value.message == "one or more listeners of event Symbol(parallel) failed"
    )


@pytest.mark.parametrize("mode", ["emit", "bail"])
def test_a_dispatch_without_listeners_does_nothing(root: Context, mode: str) -> None:
    assert getattr(root, mode)("nobody") is None


def test_listeners_do_not_run_after_their_effect_is_reverted_by_the_fiber(
    root: Context,
) -> None:
    callback = Recorder()
    root.on(event, callback)

    root.fiber.dispose()
    root.emit(event)

    assert callback.calls == []
    assert hook_snapshot(root) == {}


@pytest.mark.parametrize("flag", [True, False])
def test_a_global_listener_ignores_the_dispatch_filter(
    root: Context, flag: bool
) -> None:
    callback = Recorder()
    root.extend(filter=Filter(True).filter).on(
        event, callback, EventOptions(is_global=True)
    )

    root.emit(event, this=Session(flag))

    assert len(callback.calls) == 1


def test_a_prepended_listener_runs_first(root: Context) -> None:
    order: list[str] = []
    root.on(event, lambda: order.append("a"))
    root.on(event, lambda: order.append("b"), True)
    root.on(event, lambda: order.append("c"), EventOptions(prepend=True))

    root.emit(event)

    assert order == ["c", "b", "a"]


def test_the_dispatch_report_lists_mode_name_and_arguments(root: Context) -> None:
    report = Recorder()
    root.on("internal/dispatch", report)
    this = Session(True)

    root.emit("hello", 1, this=this)
    root.emit("internal/hello")

    assert report.calls == [("emit", "hello", (1,), this)]


def test_an_internal_listener_event_can_replace_a_registration(root: Context) -> None:
    replacement = root.effect(lambda: None, "replacement")
    seen = Recorder(lambda ctx, name, listener, options: replacement)
    root.on("internal/listener", seen)

    assert root.on(event, lambda: None) is replacement
    assert hook_snapshot(root) == {"internal/listener": 1}
    assert seen.calls[0][1] == event


@pytest.mark.parametrize("mode", ["bail", "serial"])
@pytest.mark.parametrize(
    ("result", "stops"),
    [(None, False), (False, False), (0, True), ("", True), (True, True), ("x", True)],
)
def test_only_none_and_false_let_the_next_listener_run(
    root: Context, mode: str, result: object, stops: bool
) -> None:
    after = Recorder()
    root.on(event, lambda: result)
    root.on(event, after)

    outcome = getattr(root, mode)(event)
    value = outcome.result() if isinstance(outcome, Deferred) else outcome

    assert (len(after.calls) == 0) is stops
    assert value == (result if stops else None)


def test_serial_waits_for_an_async_listener_before_the_next_one(
    root: Context, clock: FakeClock
) -> None:
    order: list[str] = []

    async def slow() -> None:
        await clock.sleep(10)
        order.append("slow")

    root.on(event, slow)
    root.on(event, lambda: order.append("next"))

    result = root.serial(event)
    assert order == [] and not result.done()
    clock.advance(10)

    assert order == ["slow", "next"] and result.done()
