"""Isolation: realms, shared labels, and the injected view at an isolation boundary."""

import logging
from collections.abc import Callable, Coroutine
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import Recorder, event

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.service import Service

NO_INJECT = 'cannot get property "foo" without inject'

type Run = Callable[[Coroutine[Any, Any, Any]], Deferred[Any]]


def read(ctx: Context) -> object:
    """Return the `value` of the service `foo`, or the message of the access error."""
    try:
        return ctx["foo"]["value"]
    except CordisError as error:
        return str(error)


@pytest.mark.upstream(
    "isolate.spec.ts::Isolation::does not expose own provisions across isolation boundaries"  # noqa: E501
)
def test_own_provisions_stay_inside_the_isolated_context(
    root: Context, run: Run
) -> None:
    async def outer(ctx: Context, config: object) -> None:
        isolated = ctx.isolate("foo")
        isolated.provide("foo", {})
        assert isolated["foo"] is not None
        with pytest.raises(CordisError, match=NO_INJECT):
            ctx["foo"]

        def inner(ctx: Context, config: object) -> None:
            assert ctx["foo"] is not None

        await isolated.use(inner)

        def sibling(ctx: Context, config: object) -> None:
            with pytest.raises(CordisError, match=NO_INJECT):
                ctx["foo"]

        await ctx.use(sibling)

    async def body() -> None:
        await root.use(outer)
        await root.fiber.dispose()

    run(body()).result()


@pytest.mark.upstream(
    "isolate.spec.ts::Isolation::does not inherit the injected view across isolation boundaries"  # noqa: E501
)
def test_the_injected_view_ends_at_an_isolation_boundary(
    root: Context, run: Run
) -> None:
    root.provide("foo", {"value": "outer"})
    results: list[object] = []

    async def body(ctx: Context) -> None:
        await ctx.use(lambda ctx, config: results.append(read(ctx)))
        await ctx.isolate("foo").use(lambda ctx, config: results.append(read(ctx)))
        # pinning the realm that is already there still makes an isolation of its own
        pinned = ctx.isolate("foo", ctx.isolates["foo"])
        await pinned.use(lambda ctx, config: results.append(read(ctx)))

    root.inject(["foo"], lambda ctx: body(ctx)).wait().result()

    assert results == ["outer", NO_INJECT, NO_INJECT]


@pytest.mark.upstream("isolate.spec.ts::Isolation::isolated context")
def test_each_isolated_context_loads_against_its_own_provider(root: Context) -> None:
    callback, dispose = Recorder(), Recorder()
    component = SimpleNamespace(
        inject=["foo"], apply=lambda ctx, config: (callback(), dispose)[1]
    )

    root.use(component)
    ctx1 = root.isolate("foo")
    ctx1.use(component)
    ctx2 = root.isolate("foo")
    ctx2.use(component)

    dispose0 = root.provide("foo", {"bar": 100})
    assert root.get("foo") == {"bar": 100}
    assert ctx1.get("foo") is None
    assert ctx2.get("foo") is None
    assert (len(callback.calls), len(dispose.calls)) == (1, 0)

    ctx1.provide("foo", {"bar": 200})
    assert (root.get("foo"), ctx1.get("foo")) == ({"bar": 100}, {"bar": 200})
    assert ctx2.get("foo") is None
    assert (len(callback.calls), len(dispose.calls)) == (2, 0)

    dispose0()
    assert root.get("foo") is None
    assert ctx1.get("foo") == {"bar": 200}
    assert (len(callback.calls), len(dispose.calls)) == (2, 1)

    ctx2.provide("foo", {"bar": 300})
    assert root.get("foo") is None
    assert ctx2.get("foo") == {"bar": 300}
    assert (len(callback.calls), len(dispose.calls)) == (3, 1)


@pytest.mark.upstream("isolate.spec.ts::Isolation::shared label")
def test_contexts_with_one_label_share_a_provider(root: Context) -> None:
    callback, dispose = Recorder(), Recorder()
    component = SimpleNamespace(
        inject=["foo"], apply=lambda ctx, config: (callback(), dispose)[1]
    )
    label = object()

    root.use(component)
    ctx1 = root.isolate("foo", label)
    ctx1.use(component)
    ctx2 = root.isolate("foo", label)
    ctx2.use(component)
    assert len(callback.calls) == 0

    root.provide("foo", {"bar": 100})
    assert (ctx1.get("foo"), ctx2.get("foo")) == (None, None)
    assert (len(callback.calls), len(dispose.calls)) == (1, 0)

    dispose12 = ctx1.provide("foo", {"bar": 200})
    assert root.get("foo") == {"bar": 100}
    assert (ctx1.get("foo"), ctx2.get("foo")) == ({"bar": 200}, {"bar": 200})
    assert (len(callback.calls), len(dispose.calls)) == (3, 0)

    dispose12()
    assert (ctx1.get("foo"), ctx2.get("foo")) == (None, None)
    assert (len(callback.calls), len(dispose.calls)) == (3, 2)


@pytest.mark.upstream("isolate.spec.ts::Isolation::isolated event")
def test_a_service_event_reaches_only_the_listeners_of_its_realm(
    root: Context,
) -> None:
    class Foo(Service):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "foo")
            self.ctx.emit(event, this=self)

    ctx = root.isolate("foo")
    outer, inner = Recorder(), Recorder()
    root.on(event, outer)
    ctx.on(event, inner)

    ctx.use(Foo)

    assert (len(outer.calls), len(inner.calls)) == (0, 1)


@pytest.mark.upstream(
    "isolate.spec.ts::Isolation::keeps own provisions separate from the injected view"
)
def test_own_provisions_do_not_replace_the_injected_view(
    root: Context, run: Run, caplog: pytest.LogCaptureFixture
) -> None:
    root.provide("foo", {"value": "outer"})
    closed: list[object] = []

    async def body(ctx: Context) -> Any:
        first, second = ctx.isolate("foo"), ctx.isolate("foo")
        withdraw = first.provide("foo", {"value": "first"})
        second.provide("foo", {"value": "second"})
        assert [read(c) for c in (ctx, first, second)] == ["outer", "first", "second"]
        for realm in (first, second):
            await realm.use(lambda c, config: closed.append(read(c)))
        assert closed == ["first", "second"]

        await withdraw()
        assert read(ctx) == "outer"
        assert read(first) == NO_INJECT
        assert read(second) == "second"
        return lambda: closed.append(read(ctx))

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber = root.inject(["foo"], lambda ctx: body(ctx))
        fiber.wait().result()
        fiber.dispose().result()
        root.fiber.dispose().result()

    assert closed[-1] == "outer"
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
