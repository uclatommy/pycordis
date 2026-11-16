"""The `Service` base: providing on construction, hooks, and the realm filter."""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from conftest import Counter, FakeClock, Recorder, event, hook_snapshot

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.service import Service


class Foo(Service[Any]):
    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "foo")


@pytest.mark.upstream("service.spec.ts::Service::pending inject")
def test_a_dependent_waits_for_an_asynchronous_init_hook(root: Context) -> None:
    gate = Deferred[None]()

    class Slow(Foo):
        async def cordis_init(self) -> None:
            self.ctx.on(event, lambda: gate.resolve(None))
            await gate

    callback = Recorder()
    root.inject(["foo"], callback)
    assert len(callback.calls) == 0

    root.use(Slow)
    assert len(callback.calls) == 0

    root.emit(event)
    assert len(callback.calls) == 1


class Counting(Service[Any]):
    """A service whose methods change a counter that it may or may not inject."""

    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "foo")

    @property
    def value(self) -> int:
        return self.ctx["counter"].value

    def increase(self) -> Any:
        return self.ctx["counter"].increase()


class InjectingCounting(Counting):
    inject = ["counter"]


@pytest.mark.upstream("service.spec.ts::Service::traceable effect (with inject)")
@pytest.mark.upstream("service.spec.ts::Service::traceable effect (without inject)")
@pytest.mark.parametrize("component", [InjectingCounting, Counting])
def test_an_effect_through_a_root_view_outlives_the_fibers_that_read_it(
    root: Context, component: type[Counting]
) -> None:
    root.provide("counter", Counter(root))
    root.use(component)
    root["foo"].increase()
    assert root["foo"].value == 1
    seen: list[int] = []

    def reader(ctx: Context) -> None:
        root["foo"].increase()
        seen.append(root["foo"].value)

    fiber = root.inject(["foo"], reader)
    fiber.dispose()
    root["foo"].increase()

    assert seen == [2]
    assert root["foo"].value == 3


def test_an_effect_through_a_view_belongs_to_the_fiber_that_reads_it(
    root: Context,
) -> None:
    root.provide("counter", Counter(root))
    root.use(InjectingCounting)

    fiber = root.inject(["foo"], lambda ctx: ctx["foo"].increase())
    assert root["foo"].value == 1

    fiber.dispose()
    assert root["foo"].value == 0


@pytest.mark.upstream("service.spec.ts::Service::compare snapshot")
def test_disposing_a_service_restores_the_hook_tables(root: Context) -> None:
    class Test(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "test")
            ctx.inject(["test"], lambda ctx: None)

    before = hook_snapshot(root)
    fiber = root.use(Test)
    after = hook_snapshot(root)

    fiber.dispose()
    assert hook_snapshot(root) == before

    root.use(Test)
    assert hook_snapshot(root) == after


@pytest.mark.upstream("service.spec.ts::Service::multiple injects")
def test_each_init_hook_runs_once_when_services_load_in_any_order(
    root: Context,
) -> None:
    foo, bar, qux = Recorder(), Recorder(), Recorder()

    class Dependent(Foo):
        inject = ["qux"]
        cordis_init = foo

    class Chained(Service[Any]):
        inject = ["foo", "qux"]
        cordis_init = bar

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "bar")

    class Base(Service[Any]):
        cordis_init = qux

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "qux")

    root.use(Dependent)
    root.use(Chained)
    root.use(Base)

    assert [len(foo.calls), len(bar.calls), len(qux.calls)] == [1, 1, 1]


@pytest.mark.upstream(
    "service.spec.ts::Service[Symbol.hasInstance]::answers for null and undefined"
)
@pytest.mark.upstream(
    "service.spec.ts::Service[Symbol.hasInstance]::"
    "answers for a plain object after walking to the end of the chain"
)
@pytest.mark.parametrize("value", [None, {}, [], object(), Foo])
def test_a_value_that_is_not_a_service_is_not_an_instance(value: object) -> None:
    assert not isinstance(value, Service)


class Named(Service[Any]):
    provide = "named"


@pytest.mark.parametrize(
    ("make", "name"),
    [(lambda ctx: Foo(ctx), "foo"), (lambda ctx: Named(ctx), "named")],
    ids=["constructor argument", "provide attribute"],
)
def test_the_constructor_provides_the_service_under_its_name(
    root: Context, make: Callable[[Context], Service[Any]], name: str
) -> None:
    service = make(root)

    view = root.get(name)
    assert isinstance(view, Service)
    assert view.cordis_original is service
    assert (service.ctx, service.name) == (root, name)
    assert isinstance(service, Service)


def test_a_service_without_a_name_is_a_type_error(root: Context) -> None:
    class Anonymous(Service[Any]):
        pass

    with pytest.raises(TypeError, match="needs a service name"):
        Anonymous(root)


def test_a_second_provider_in_one_realm_is_rejected(root: Context) -> None:
    Foo(root)

    with pytest.raises(CordisError, match='service "foo" has been registered at'):
        Foo(root)


@pytest.mark.parametrize(
    ("outcome", "expected"), [(True, 1), (False, 0), (RuntimeError("check"), 0)]
)
def test_a_dependent_loads_only_while_the_check_hook_accepts(
    root: Context,
    caplog: pytest.LogCaptureFixture,
    outcome: object,
    expected: int,
) -> None:
    checked_from: list[Any] = []

    class Checked(Foo):
        def cordis_check(self) -> bool:
            checked_from.append(self.ctx.fiber)
            if isinstance(outcome, Exception):
                raise outcome
            return bool(outcome)

    callback = Recorder()
    root.use(Checked)
    fiber = root.inject(["foo"], callback)

    assert len(callback.calls) == expected
    # the hook sees the context of the fiber that injects the service
    assert fiber in checked_from
    # an error of the hook is reported, and a plain rejection is not
    logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(logged) == isinstance(outcome, Exception)


def test_the_filter_hook_accepts_the_contexts_of_the_same_realm(root: Context) -> None:
    foo = Foo(root)

    assert foo.cordis_filter(root.extend())
    assert not foo.cordis_filter(root.isolate("foo"))


class Tracked(Service[Any]):
    """A service that records when it is active and when it closes."""

    def __init__(self, ctx: Context, seq: list[str]) -> None:
        super().__init__(ctx, "foo")
        self.seq = seq
        self.active = False

    def cordis_init(self) -> Any:
        self.active = True

        def close() -> None:
            self.active = False
            self.seq.append("foo")

        yield close


def dependent(seq: list[str], clock: FakeClock) -> Callable[[Context], None]:
    """A component whose teardown waits 10 ms, then reads the service."""

    def apply(ctx: Context) -> None:
        def revert() -> Deferred[None]:
            wait = clock.sleep(10)
            wait.add_done_callback(
                lambda _: seq.append(
                    "dependent" if ctx["foo"].active else "dependent (stale)"
                )
            )
            return wait

        ctx.effect(lambda: revert)

    return apply


@pytest.mark.upstream("service.spec.ts::Service::dispose dependents before provider")
def test_a_dependent_leaves_before_its_provider(
    root: Context, clock: FakeClock
) -> None:
    seq: list[str] = []

    def owner(ctx: Context, config: object) -> None:
        ctx.use(Tracked, seq)
        ctx.inject(["foo"], dependent(seq, clock))

    fiber = root.use(owner)
    disposing = fiber.dispose()
    clock.advance(10)

    assert disposing.done()
    assert seq == ["dependent", "foo"]


@pytest.mark.upstream(
    "service.spec.ts::Service::dispose dependents before provider restarts"
)
def test_a_dependent_leaves_before_its_provider_restarts(
    root: Context, clock: FakeClock
) -> None:
    seq: list[str] = []
    fiber = root.use(Tracked, seq)
    callback = Recorder(dependent(seq, clock))
    root.inject(["foo"], callback)
    assert len(callback.calls) == 1

    restarting = fiber.restart()
    clock.advance(10)

    assert restarting.done()
    assert seq == ["dependent", "foo"]
    assert len(callback.calls) == 2


@pytest.mark.upstream(
    "service.spec.ts::Service::dispose dependents before isolated provider"
)
def test_a_dependent_leaves_before_the_provider_of_its_realm(root: Context) -> None:
    label = object()
    callback = Recorder()
    dispose = Recorder()

    def consume(ctx: Context) -> Recorder:
        callback()
        return dispose

    root.isolate("foo", label).inject(["foo"], consume)
    fiber = root.use(lambda ctx, config: ctx.isolate("foo", label).provide("foo", {}))
    assert len(callback.calls) == 1

    fiber.dispose().result()

    assert len(dispose.calls) == 1
