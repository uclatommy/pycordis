"""The `inject` decorator on classes and on methods."""

from typing import Any

import pytest
from conftest import Recorder

from pycordis.context import Context
from pycordis.registry import inject
from pycordis.service import Service


class Foo(Service[Any]):
    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "foo")


@pytest.mark.upstream("decorator.spec.ts::Decorator::@Inject on class method")
def test_a_decorated_method_runs_while_its_service_is_provided(root: Context) -> None:
    callback, dispose = Recorder(), Recorder()

    class Bar(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "bar")

        @inject("foo")
        def method(self) -> Any:
            callback()
            return dispose

    root.use(Bar)
    assert (len(callback.calls), len(dispose.calls)) == (0, 0)

    fiber = root.use(Foo)
    assert (len(callback.calls), len(dispose.calls)) == (1, 0)

    fiber.dispose()
    assert (len(callback.calls), len(dispose.calls)) == (1, 1)


def test_a_decorated_method_sees_the_context_of_its_own_fiber(root: Context) -> None:
    seen: list[Context] = []

    class Bar(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "bar")

        @inject("foo")
        def method(self) -> None:
            seen.append(self.ctx)
            self.ctx["foo"]

    root.use(Foo)
    root.use(Bar)

    assert len(seen) == 1
    assert seen[0].fiber is not root.fiber


def test_stacked_method_decorators_need_every_service(root: Context) -> None:
    callback = Recorder()

    class Bar(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "bar")

        @inject("foo")
        @inject("qux")
        def method(self) -> None:
            callback()

    root.use(Bar)
    root.use(Foo)
    assert len(callback.calls) == 0

    root.provide("qux", object())
    assert len(callback.calls) == 1


def test_a_class_decorator_extends_a_copy_of_the_inherited_mapping() -> None:
    class Base:
        inject = {"a": {"x": 1}}

    @inject("b", {"y": 2})
    class Child(Base):
        pass

    @inject("c")
    class Plain:
        pass

    assert list(Child.inject) == ["a", "b"]
    assert Child.inject["b"] == {"y": 2}
    assert Base.inject == {"a": {"x": 1}}
    assert Plain.inject == {"c": None}


def test_a_class_decorator_turns_a_list_into_a_mapping() -> None:
    @inject("b")
    class Child:
        inject = ["a"]

    assert Child.inject == {"a": None, "b": None}


@pytest.mark.parametrize("target", [42, "text", None])
def test_the_decorator_rejects_anything_but_a_class_or_a_function(target: Any) -> None:
    with pytest.raises(
        TypeError, match=r"@Inject\(\) can only be used on class or class methods"
    ):
        inject("foo")(target)


def test_a_method_that_a_subclass_overrides_without_the_decorator_does_not_run(
    root: Context,
) -> None:
    callback = Recorder()

    class Base(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "bar")

        @inject("foo")
        def method(self) -> None:
            callback()

    class Child(Base):
        def method(self) -> None:
            callback()

    root.use(Foo)
    root.use(Child)

    assert len(callback.calls) == 0


def test_a_decorated_method_of_a_plain_class_runs_on_its_instance(
    root: Context,
) -> None:
    seen: list[object] = []

    class Plain:
        def __init__(self, ctx: Context, config: object = None) -> None:
            pass

        @inject("foo")
        def method(self) -> None:
            seen.append(self)

    root.use(Plain)
    assert seen == []

    root.use(Foo)
    assert len(seen) == 1
    assert isinstance(seen[0], Plain)
