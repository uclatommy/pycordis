"""Associated access: a view reads `"<service>.<name>"` for the attribute `name`."""

from collections.abc import Callable
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.errors import CordisError
from pycordis.service import Service
from pycordis.traceable import Tracker


class Foo(Service[Any]):
    qux = 1

    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "foo")


class FooBar(Service[Any]):
    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "foo.bar")

    def hello(self) -> str:
        return "bar"


class Session:
    """A plain object that a service hands out, with the context of its holder."""

    cordis_tracker = Tracker(property="ctx", associate="session")

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.user = "ann"


@pytest.mark.upstream("associate.spec.ts::Association::service injection")
def test_an_attribute_reads_the_associated_service(root: Context) -> None:
    root.use(Foo)
    fiber = root.use(FooBar)

    assert isinstance(root["foo"], Foo)
    assert isinstance(root["foo"].bar, FooBar)
    assert root["foo"].qux == 1

    fiber.dispose()
    assert root["foo"].bar is None


@pytest.mark.upstream("associate.spec.ts::Association::property injection")
def test_an_attribute_assignment_sets_the_associated_service(root: Context) -> None:
    root.use(Foo)
    root.provide("foo.bar")
    root.provide("foo.baz")
    root["foo"].qux = 2
    root["foo"].bar = 3
    callback = lambda: "baz"  # noqa: E731
    root["foo"].baz = callback

    seen: list[Any] = []

    def reader(ctx: Context) -> None:
        seen.extend([ctx["foo"].qux, ctx["foo"].bar, ctx["foo.bar"], ctx["foo"].baz])
        with pytest.raises(CordisError, match='cannot get property "foo.qux"'):
            ctx["foo.qux"]

    root.inject(["foo"], reader)

    assert seen == [2, 3, 3, callback]


@pytest.mark.upstream(
    "associate.spec.ts::Association::associated type - service injection"
)
def test_a_service_adds_a_method_to_an_associated_type(root: Context) -> None:
    class Maker(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "maker")

        def create_session(self) -> Session:
            return Session(self.ctx)

    class Bar(Service[Any]):
        inject = ["maker"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "bar")
            ctx.mixin("bar", {"answer": "session.answer", "greet": "session.greet"})

        def answer(self) -> int:
            return 42

        def greet(self) -> str:
            # `self` sees the service and the session that it extends
            return f"{self.name} {self.user}"

    root.use(Maker)
    seen: list[Any] = []

    def outer(ctx: Context) -> None:
        session = ctx["maker"].create_session()
        seen.extend([isinstance(session, Session), hasattr(session, "answer")])
        ctx.use(Bar)
        ctx.inject(["bar"], inner)

    def inner(ctx: Context) -> None:
        session = ctx["maker"].create_session()
        seen.extend([isinstance(session, Session), session.answer(), session.greet()])

    root.inject(["maker"], outer)

    assert seen == [True, False, True, 42, "bar ann"]


@pytest.mark.upstream(
    "associate.spec.ts::Association::associated type - accessor injection"
)
def test_a_property_of_a_service_reads_and_writes_through_an_associated_type(
    root: Context,
) -> None:
    class Maker(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "maker")

        def session(self) -> Session:
            return Session(self.ctx)

    class Bar(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "bar")
            ctx.mixin("bar", {"bar": "session.bar"})

        @property
        def bar(self) -> int | None:
            return getattr(self, "secret", None)

        @bar.setter
        def bar(self, value: int) -> None:
            self.secret = value + 1

    root.use(Maker)
    root.use(Bar)
    seen: list[Any] = []

    def outer(ctx: Context) -> None:
        session = ctx["maker"].session()
        with pytest.raises(CordisError, match='cannot get property "bar"'):
            _ = session.bar
        ctx.inject(["bar"], inner)

    def inner(ctx: Context) -> None:
        session = ctx["maker"].session()
        seen.append(session.bar)
        session.bar = 100
        # the state belongs to the session, not to the service
        seen.extend([session.bar, session.cordis_original.secret])

    root.inject(["maker"], outer)

    assert seen == [None, 101, 101]
    assert not hasattr(root["bar"].cordis_original, "secret")


@pytest.mark.upstream(
    "associate.spec.ts::Association::associated access follows the service fiber"
)
def test_an_associated_access_follows_the_fiber_of_the_service(root: Context) -> None:
    class Probe(Service[Any]):
        def probe(self) -> str:
            return self.ctx["foo"].bar.hello()

    class Baz(Probe):
        inject = ["foo", "foo.bar"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "baz")

    class Qux(Probe):
        inject = ["foo"]  # `foo.bar` is not injected

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "qux")

    for component in (Foo, FooBar, Baz, Qux):
        root.use(component)
    message = 'cannot get property "foo.bar" without inject'
    results: list[Callable[[], str]] = []

    root.inject(
        ["baz", "qux"], lambda ctx: results.extend([ctx["baz"].probe, ctx["qux"].probe])
    )

    assert root["baz"].probe() == "bar"
    with pytest.raises(CordisError, match=message):
        root["qux"].probe()
    assert results[0]() == "bar"
    with pytest.raises(CordisError, match=message):
        results[1]()


@pytest.mark.upstream("associate.spec.ts::Association::inspect")
def test_a_view_passes_arguments_through_unchanged(root: Context) -> None:
    class Inspector(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "foo")

        def first(self, arg: object) -> object:
            return self.second(arg)

        def second(self, arg: object) -> object:
            return arg

    root.use(Inspector)
    marker = type("X", (), {})

    assert root["foo"].first(marker) is marker
