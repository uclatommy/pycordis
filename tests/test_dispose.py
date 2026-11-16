"""Ported upstream effect-disposal cases that need only the root context."""

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from conftest import FakeClock, event

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import Disposer, EffectMeta

Run = Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]


@pytest.mark.upstream("dispose.spec.ts::Effects::dispose manually")
def test_manual_dispose_runs_the_inverse_once(root: Context) -> None:
    calls: list[int] = []
    dispose = root.effect(lambda: lambda: calls.append(1))
    assert root.fiber.get_effects() == [EffectMeta("anonymous", [])]
    assert calls == []

    dispose()
    dispose()

    assert calls == [1]


@pytest.mark.upstream("dispose.spec.ts::Effects::async return 1")
def test_async_return_collects_the_inverse_when_the_setup_ends(
    root: Context, clock: FakeClock
) -> None:
    seq: list[int] = []

    async def setup() -> Callable[[], None]:
        await clock.sleep(100)
        seq.append(1)
        return lambda: seq.append(2)

    dispose = root.effect(setup)
    assert seq == []
    clock.advance(100)
    assert seq == [1]

    pending = dispose()

    assert pending is not None and pending.done()
    assert seq == [1, 2]


@pytest.mark.upstream("dispose.spec.ts::Effects::async return 2")
def test_dispose_during_an_async_setup_reverts_when_the_setup_ends(
    root: Context, clock: FakeClock
) -> None:
    seq: list[int] = []

    async def setup() -> Callable[[], None]:
        await clock.sleep(100)
        seq.append(1)
        return lambda: seq.append(2)

    dispose = root.effect(setup)
    dispose()
    assert seq == []

    clock.advance(100)

    assert seq == [1, 2]


def _three_step_setup(clock: FakeClock, seq: list[int]) -> Callable[[], Any]:
    async def setup() -> Any:
        await clock.sleep(100)
        seq.append(1)
        yield lambda: seq.append(2)
        await clock.sleep(100)
        seq.append(3)
        yield lambda: seq.append(4)
        await clock.sleep(100)
        seq.append(5)
        yield lambda: seq.append(6)

    return setup


@pytest.mark.upstream("dispose.spec.ts::Effects::async yield 1")
def test_async_yield_reverts_every_yielded_inverse_newest_first(
    root: Context, clock: FakeClock
) -> None:
    seq: list[int] = []
    dispose = root.effect(_three_step_setup(clock, seq))
    assert seq == []

    clock.advance(300)
    assert seq == [1, 3, 5]
    dispose()

    assert seq == [1, 3, 5, 6, 4, 2]


@pytest.mark.upstream("dispose.spec.ts::Effects::async yield 2 (aborted)")
@pytest.mark.upstream("dispose.spec.ts::Effects::async yield 3 (aborted)")
@pytest.mark.parametrize(
    ("abort_at", "before", "after"),
    [(50, [], [1, 2]), (100, [1], [1, 3, 4, 2])],
)
def test_async_yield_stops_at_the_first_step_after_dispose(
    root: Context,
    clock: FakeClock,
    abort_at: int,
    before: list[int],
    after: list[int],
) -> None:
    seq: list[int] = []
    dispose = root.effect(_three_step_setup(clock, seq))
    clock.advance(abort_at)
    assert seq == before

    dispose()
    assert seq == before
    clock.advance(300)

    assert seq == after


@pytest.mark.upstream("dispose.spec.ts::Effects::async yield (aborted, finally)")
def test_async_yield_aborted_runs_the_finally_block_of_the_iterator(
    root: Context, clock: FakeClock
) -> None:
    seq: list[int] = []

    async def setup() -> Any:
        try:
            yield lambda: seq.append(1)
            await clock.sleep(100)
            yield lambda: seq.append(2)
            await clock.sleep(100)
            yield lambda: seq.append(3)
        finally:
            seq.append(0)

    dispose = root.effect(setup)
    clock.advance(50)
    disposing = dispose()
    clock.advance(100)

    assert disposing is not None and disposing.done()
    assert seq == [0, 2, 1]


@pytest.mark.upstream("dispose.spec.ts::Effects::async yield 4 (await dispose)")
def test_awaiting_the_handle_gives_a_disposer_after_the_setup_ends(
    root: Context, clock: FakeClock, run: Run
) -> None:
    seq: list[int] = []
    handle = root.effect(_three_step_setup(clock, seq))

    async def body() -> Callable[[], Any]:
        return await handle

    waiting = run(body())
    assert not waiting.done()
    clock.advance(300)
    assert seq == [1, 3, 5]
    waiting.result()()

    assert seq == [1, 3, 5, 6, 4, 2]


@pytest.mark.upstream("dispose.spec.ts::Effects::return with error")
@pytest.mark.upstream("dispose.spec.ts::Effects::yield with error")
@pytest.mark.parametrize("form", ["return", "yield"])
def test_a_sync_setup_that_raises_reverts_what_it_collected(
    root: Context, form: str
) -> None:
    seq: list[int] = []

    def returning() -> Any:
        raise RuntimeError("test")

    def yielding() -> Any:
        yield lambda: seq.append(1)
        raise RuntimeError("test")

    with pytest.raises(RuntimeError, match="test"):
        root.effect(returning if form == "return" else yielding)

    assert seq == ([] if form == "return" else [1])
    assert root.fiber.get_effects() == []


@pytest.mark.upstream("dispose.spec.ts::Effects::async return with error")
@pytest.mark.upstream("dispose.spec.ts::Effects::async yield with error")
@pytest.mark.upstream("dispose.spec.ts::Effects::async yield invalid effect")
@pytest.mark.parametrize(
    ("form", "error", "expected"),
    [
        ("return", RuntimeError, []),
        ("yield", RuntimeError, [1]),
        ("invalid", TypeError, [0, 1]),
    ],
)
def test_an_async_setup_that_fails_rejects_the_handle_and_reverts(
    root: Context, run: Run, form: str, error: type[Exception], expected: list[int]
) -> None:
    seq: list[int] = []

    async def returning() -> Any:
        raise RuntimeError("test")

    async def yielding() -> Any:
        yield lambda: seq.append(1)
        raise RuntimeError("test")

    async def invalid() -> Any:
        try:
            yield lambda: seq.append(1)
            yield 1
        finally:
            seq.append(0)

    handle = root.effect(
        {"return": returning, "yield": yielding, "invalid": invalid}[form]
    )

    async def body() -> None:
        await handle

    with pytest.raises(error):
        run(body()).result()
    assert seq == expected


@pytest.mark.upstream("dispose.spec.ts::Effects::yield dispose")
def test_a_sync_iterator_effect_reverts_its_parts_newest_first(root: Context) -> None:
    seq: list[int] = []

    def inner() -> Iterator[Disposer]:
        yield root.on(event, lambda: None)
        yield lambda: seq.append(3)

    def setup() -> Iterator[Disposer]:
        yield lambda: seq.append(1)
        yield root.on(event, lambda: None)
        yield lambda: seq.append(2)
        yield root.effect(inner)

    dispose = root.effect(setup)
    root.on(event, lambda: None)

    # only the effects at the top level of the fiber are listed
    on = EffectMeta(f'ctx.on("{event}")', [])
    assert root.fiber.get_effects() == [
        EffectMeta("anonymous", [on, EffectMeta("anonymous", [on])]),
        on,
    ]
    assert seq == []
    dispose()
    assert seq == [3, 2, 1]
    dispose()
    assert seq == [3, 2, 1]
