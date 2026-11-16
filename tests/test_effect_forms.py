"""Tests for the effect forms, effect handles, and root disposal."""

import contextlib
import gc
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import EffectMeta, FiberState

Run = Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]


def _disposer_form(seq: list[str]) -> Callable[[], Any]:
    return lambda: lambda: seq.append("a")


def _iterator_form(seq: list[str]) -> Callable[[], Any]:
    def setup() -> Any:
        yield lambda: seq.append("a")
        yield None
        yield lambda: seq.append("b")

    return setup


def _iterator_return_form(seq: list[str]) -> Callable[[], Any]:
    def setup() -> Any:
        yield lambda: seq.append("a")
        return lambda: seq.append("b")

    return setup


def _async_iterator_form(seq: list[str]) -> Callable[[], Any]:
    async def setup() -> Any:
        yield lambda: seq.append("a")
        await Deferred.resolved(None)
        yield lambda: seq.append("b")

    return setup


def _coroutine_form(seq: list[str]) -> Callable[[], Any]:
    async def setup() -> Any:
        await Deferred.resolved(None)
        return lambda: seq.append("a")

    return setup


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        (_disposer_form, ["a"]),
        (_iterator_form, ["b", "a"]),
        (_iterator_return_form, ["b", "a"]),
        (_async_iterator_form, ["b", "a"]),
        (_coroutine_form, ["a"]),
    ],
    ids=["disposer", "iterator", "iterator-return", "async-iterator", "coroutine"],
)
def test_each_form_reverts_newest_first_and_the_handle_awaits_the_setup(
    root: Context, run: Run, make: Callable[[list[str]], Any], expected: list[str]
) -> None:
    seq: list[str] = []
    handle = root.effect(make(seq))

    async def body() -> Callable[[], Any]:
        return await handle

    revert = run(body()).result()
    assert seq == []
    revert()
    handle()

    assert seq == expected


def test_a_none_return_registers_an_effect_without_inverses(root: Context) -> None:
    handle = root.effect(lambda: None, "empty")

    assert root.fiber.get_effects() == [EffectMeta("empty", [])]
    assert handle() is None


@pytest.mark.parametrize(
    "value",
    [1, "text", object(), contextlib.nullcontext(), [lambda: None]],
    ids=["int", "str", "object", "context-manager", "list"],
)
def test_an_invalid_return_raises_type_error(root: Context, value: object) -> None:
    with pytest.raises(TypeError, match="Invalid effect"):
        root.effect(lambda: value)  # type: ignore[arg-type, return-value]

    assert root.fiber.get_effects() == []


def test_an_invalid_sync_yield_reverts_and_closes_the_iterator(root: Context) -> None:
    seq: list[str] = []

    def setup() -> Any:
        try:
            yield lambda: seq.append("inverse")
            yield 1
        finally:
            seq.append("finally")

    with pytest.raises(TypeError, match="Invalid effect"):
        root.effect(setup)

    assert seq == ["finally", "inverse"]


def test_an_inverse_that_returns_a_deferred_holds_back_the_next_inverse(
    root: Context,
) -> None:
    seq: list[str] = []
    gate = Deferred[None]()

    def second() -> Deferred[None]:
        seq.append("second")
        return gate

    def setup() -> Any:
        yield lambda: seq.append("first")
        yield second

    handle = root.effect(setup)

    pending = handle()
    assert pending is not None and not pending.done()
    assert seq == ["second"]
    gate.resolve(None)

    assert pending.done()
    assert seq == ["second", "first"]


def test_child_effects_are_listed_under_the_effect_that_collected_them(
    root: Context,
) -> None:
    def setup() -> Any:
        yield root.effect(lambda: None, "one")
        yield root.effect(lambda: None, "two")

    root.effect(lambda: None, "sibling")
    root.effect(setup, "parent")

    assert root.fiber.get_effects() == [
        EffectMeta("sibling", []),
        EffectMeta("parent", [EffectMeta("one", []), EffectMeta("two", [])]),
    ]


def test_reverting_a_parent_effect_reverts_its_child_effects(root: Context) -> None:
    seq: list[str] = []

    def setup() -> Any:
        yield root.effect(lambda: lambda: seq.append("child"), "child")
        yield lambda: seq.append("parent")

    parent = root.effect(setup)
    parent()

    assert seq == ["parent", "child"]
    assert root.fiber.get_effects() == []


def test_dropping_the_handle_does_not_revert_the_effect(root: Context) -> None:
    seq: list[str] = []
    root.effect(lambda: lambda: seq.append("reverted"), "kept")
    gc.collect()

    assert seq == []
    assert root.fiber.get_effects() == [EffectMeta("kept", [])]


@pytest.mark.parametrize("kind", ["sync", "async"])
def test_an_effect_error_carries_the_call_site_as_a_note(
    root: Context, run: Run, kind: str
) -> None:
    def failing() -> Any:
        raise RuntimeError("boom")

    async def failing_async() -> Any:
        raise RuntimeError("boom")

    if kind == "sync":
        with pytest.raises(RuntimeError) as caught:
            root.effect(failing)
        error = caught.value
    else:
        handle = root.effect(failing_async)

        async def body() -> None:
            await handle

        with pytest.raises(RuntimeError) as caught:
            run(body()).result()
        error = caught.value

    notes = "\n".join(error.__notes__)
    assert "test_effect_forms.py" in notes
    assert "test_an_effect_error_carries_the_call_site_as_a_note" in notes


@pytest.mark.contract("EFF-REVERT")
def test_root_dispose_reverts_every_root_effect_and_keeps_the_root_in_use(
    root: Context,
) -> None:
    seq: list[str] = []
    states: list[FiberState] = []

    def setup() -> Any:
        yield root.effect(lambda: lambda: seq.append("child"), "child")
        yield lambda: seq.append("parent")

    def first() -> None:
        seq.append("first")
        states.append(root.fiber.state)

    root.effect(lambda: first)
    root.effect(setup, "parent")
    assert root.fiber.state is FiberState.ACTIVE

    done = root.fiber.dispose()

    assert done.done()
    assert seq == ["parent", "child", "first"]
    assert states == [FiberState.UNLOADING]
    assert root.fiber.get_effects() == []
    assert root.fiber.uid == 0
    assert root.fiber.state is FiberState.ACTIVE

    # the root is in use again, and the next disposal reverts its next effect
    root.effect(lambda: lambda: seq.append("again"))
    root.fiber.dispose()
    assert seq[-1] == "again"


def test_root_dispose_reverts_an_effect_with_an_async_setup(root: Context) -> None:
    seq: list[str] = []
    root.effect(_async_iterator_form(seq))

    root.fiber.dispose()

    assert seq == ["b", "a"]
    assert root.fiber.get_effects() == []


def _starting(started: list[str], name: str, gate: Deferred[None]) -> Any:
    def inverse() -> Deferred[None]:
        started.append(name)
        return gate

    return inverse


def test_root_dispose_waits_for_inverses_that_finish_later_and_starts_them_together(
    root: Context,
) -> None:
    started: list[str] = []
    gates = {"x": Deferred[None](), "y": Deferred[None]()}
    for name, gate in gates.items():
        root.effect(
            lambda name=name, gate=gate: lambda: (started.append(name), gate)[1]
        )

    done = root.fiber.dispose()

    assert started == ["y", "x"]
    assert not done.done()
    assert root.fiber.state is FiberState.UNLOADING
    gates["x"].resolve(None)
    assert not done.done()
    gates["y"].resolve(None)
    assert done.done()
    assert root.fiber.state is FiberState.ACTIVE


def test_a_failing_inverse_is_logged_with_its_label_and_the_others_still_run(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    seq: list[str] = []

    def explode() -> None:
        raise RuntimeError("inverse failed")

    root.effect(lambda: lambda: seq.append("before"), "ok-1")
    root.effect(lambda: explode, "bad-effect")
    root.effect(lambda: lambda: seq.append("after"), "ok-2")

    with caplog.at_level(logging.ERROR, logger="pycordis.root"):
        done = root.fiber.dispose()

    assert done.done()
    assert seq == ["after", "before"]
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "bad-effect" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[1] is not None
    assert str(record.exc_info[1]) == "inverse failed"


@pytest.mark.divergence("root-late-effect")
def test_an_effect_registered_during_root_disposal_stays_and_is_logged(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    def inverse() -> None:
        root.effect(lambda: None, "late")

    root.effect(lambda: inverse, "early")

    with caplog.at_level(logging.DEBUG, logger="pycordis.root"):
        root.fiber.dispose()

    assert root.fiber.get_effects() == [EffectMeta("late", [])]
    [record] = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert "late" in record.getMessage()


def test_an_effect_registered_outside_disposal_is_not_logged_as_late(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="pycordis.root"):
        root.effect(lambda: None, "normal")

    assert caplog.records == []


def test_wait_resolves_with_the_fiber_and_the_fiber_is_awaitable(
    root: Context, run: Run
) -> None:
    async def body() -> Any:
        return await root.fiber

    assert root.fiber.wait().result() is root.fiber
    assert run(body()).result() is root.fiber
