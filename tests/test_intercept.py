"""Intercept configuration: how a service merges `inject` and `Context.intercept`."""

from typing import Any

import pytest

from pycordis.context import Context
from pycordis.service import Service


class Foo(Service[dict[str, Any]]):
    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "foo")

    def config(self, caller: Context) -> dict[str, Any]:
        return self.cordis_resolve_intercept(caller)


@pytest.mark.upstream(
    "intercept.spec.ts::Intercept::merge context intercepts from outer to inner"
)
def test_context_intercepts_merge_from_outer_to_inner(root: Context) -> None:
    foo = Foo(root)

    ctx = root.intercept("foo", {"a": 1, "b": 1}).intercept("foo", {"b": 2})

    assert foo.config(root) == {}
    assert foo.config(ctx) == {"a": 1, "b": 2}


@pytest.mark.upstream(
    "intercept.spec.ts::Intercept::merge injected config under context intercepts"
)
def test_injected_config_merges_under_context_intercepts(root: Context) -> None:
    Foo(root)
    configs: list[dict[str, Any]] = []

    def reader(ctx: Context) -> None:
        inner = ctx.intercept("foo", {"c": 3})
        configs.append(inner["foo"].config(inner))

    root.intercept("foo", {"b": 2}).inject({"foo": {"a": 1, "b": 1}}, reader)

    assert configs == [{"a": 1, "b": 2, "c": 3}]


@pytest.mark.upstream(
    "intercept.spec.ts::Intercept::take injected config from the declaring fiber"
)
def test_the_nearest_declaring_fiber_gives_the_injected_config(root: Context) -> None:
    Foo(root)
    configs: list[dict[str, Any]] = []

    def outer(ctx: Context) -> None:
        # no declaration of its own, so the outer fiber admits the access
        ctx.use(lambda ctx, _: configs.append(ctx["foo"].config(ctx)))
        # the nearest declaration wins, and the outer one is not merged in
        ctx.inject(
            {"foo": {"b": 2}}, lambda ctx: configs.append(ctx["foo"].config(ctx))
        )

    root.inject({"foo": {"a": 1}}, outer)

    assert configs == [{"a": 1}, {"b": 2}]


@pytest.mark.upstream(
    "intercept.spec.ts::Intercept::"
    "stop looking for injected config at an isolation boundary"
)
def test_an_isolation_boundary_hides_the_injected_config(root: Context) -> None:
    Foo(root)
    label = object()
    Foo(root.isolate("foo", label))
    configs: list[dict[str, Any]] = []

    def reader(ctx: Context, _: object) -> None:
        isolated = ctx.isolate("foo", label)
        configs.append(isolated.get("foo").config(isolated))

    root.inject({"foo": {"a": 1}}, lambda ctx: ctx.use(reader))

    assert configs == [{}]


@pytest.mark.upstream(
    "intercept.spec.ts::Intercept::take no injected config for own provisions"
)
def test_a_fiber_gets_no_injected_config_for_its_own_provision(root: Context) -> None:
    Foo(root)
    configs: list[dict[str, Any]] = []

    def provider(ctx: Context) -> None:
        isolated = ctx.isolate("foo")
        own = Foo(isolated)
        # the injected config belongs to the dependency, not to the service that the
        # fiber provides itself
        intercepted = isolated.intercept("foo", {"b": 2})
        configs.extend(
            [
                ctx["foo"].config(ctx),
                own.config(isolated),
                own.config(intercepted),
            ]
        )

    root.inject({"foo": {"a": 1}}, provider)

    assert configs == [{"a": 1}, {}, {"b": 2}]


@pytest.mark.upstream("intercept.spec.ts::Intercept::custom merge")
def test_a_service_can_replace_the_merge_rule(root: Context) -> None:
    class Tagged(Foo):
        def cordis_merge_intercept(self, *configs: dict[str, Any]) -> dict[str, Any]:
            return {
                "tags": [tag for config in configs for tag in config.get("tags", [])]
            }

    Tagged(root)
    configs: list[dict[str, Any]] = []

    def reader(ctx: Context) -> None:
        inner = ctx.intercept("foo", {"tags": ["inner"]})
        configs.append(inner["foo"].config(inner))

    root.intercept("foo", {"tags": ["outer"]}).inject(
        {"foo": {"tags": ["inject"]}}, reader
    )

    assert configs == [{"tags": ["inject", "outer", "inner"]}]


def test_the_caller_defaults_to_the_context_of_the_service(root: Context) -> None:
    foo = Foo(root.intercept("foo", {"a": 1}))

    assert foo.cordis_resolve_intercept() == {"a": 1}
