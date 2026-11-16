"""The `Service` base: providing on construction, hooks, and the realm filter."""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from conftest import Recorder, event, hook_snapshot

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

    assert root.get(name) is service
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
    class Checked(Foo):
        def cordis_check(self) -> bool:
            if isinstance(outcome, Exception):
                raise outcome
            return bool(outcome)

    callback = Recorder()
    root.use(Checked)
    root.inject(["foo"], callback)

    assert len(callback.calls) == expected
    # an error of the hook is reported, and a plain rejection is not
    logged = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(logged) == isinstance(outcome, Exception)


def test_the_filter_hook_accepts_the_contexts_of_the_same_realm(root: Context) -> None:
    foo = Foo(root)

    assert foo.cordis_filter(root.extend())
    assert not foo.cordis_filter(root.isolate("foo"))
