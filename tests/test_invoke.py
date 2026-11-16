"""A service that defines `cordis_invoke` is callable."""

from typing import Any

import pytest

from pycordis.context import Context
from pycordis.service import Service


class Dependency(Service[Any]):
    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "dependency")


@pytest.mark.upstream("invoke.spec.ts::Functional Service::functional service")
def test_a_callable_service_merges_the_intercepts_of_its_use_site(
    root: Context,
) -> None:
    class Foo(Service[Any]):
        def __init__(self, ctx: Context, config: dict[str, int]) -> None:
            super().__init__(ctx, "foo")
            self.config = config

        def cordis_invoke(self, init: dict[str, int] | None = None) -> dict[str, int]:
            assert isinstance(self.ctx, Context)
            result = dict(self.config)
            for layer in self.ctx.intercept_layers("foo"):
                result.update(layer)  # type: ignore[arg-type]
            result.update(init or {})
            return result

        def invoke(self) -> dict[str, int]:
            return self()

        def extend(self, config: dict[str, int]) -> "Foo":
            return self.cordis_extend(config={**self.config, **config})

    root.use(Foo, {"a": 1})

    assert root["foo"]() == {"a": 1}
    assert root["foo"]({"z": 9}) == {"a": 1, "z": 9}
    ctx1 = root.intercept("foo", {"b": 2})
    assert ctx1["foo"]() == {"a": 1, "b": 2}
    foo1 = ctx1["foo"]
    assert isinstance(foo1, Foo)

    foo2 = root["foo"].extend({"c": 3})
    assert isinstance(foo2, Foo)
    assert foo2() == {"a": 1, "c": 3}
    foo3 = foo1.extend({"d": 4})
    assert isinstance(foo3, Foo)
    assert foo3.invoke() == {"a": 1, "b": 2, "d": 4}

    # the view of the service keeps the use site of its context
    assert foo1.invoke() == {"a": 1, "b": 2}


@pytest.mark.upstream(
    "invoke.spec.ts::Functional Service::"
    "uses the service shadow for callable extensions"
)
def test_a_callable_extension_resolves_services_at_the_def_site(root: Context) -> None:
    class Callable(Service[Any]):
        inject = ["dependency"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "callable")

        def cordis_invoke(self) -> Any:
            return self.ctx["dependency"]

        def extend(self) -> "Callable":
            return self.cordis_extend()

    class Outer(Service[Any]):
        inject = ["callable"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "outer")

        def call(self) -> list[Any]:
            callable_ = self.ctx["callable"]
            return [callable_(), callable_.extend()()]

    for component in (Dependency, Callable, Outer):
        root.use(component)
    result: list[Any] = []

    root.inject(["outer"], lambda ctx: result.extend(ctx["outer"].call()))

    assert [isinstance(item, Dependency) for item in result] == [True, True]


def test_an_extension_reads_the_attributes_that_it_does_not_set(root: Context) -> None:
    class Box(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "box")
            self.size = 1
            self.color = "red"

    root.use(Box)
    box = root["box"]
    extension = box.cordis_extend(size=2)

    assert (extension.size, extension.color) == (2, "red")
    box.color = "blue"
    assert extension.color == "blue"
    assert box.size == 1
    assert not hasattr(extension, "missing")
