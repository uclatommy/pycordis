"""The def site and the use site of a view: which context decides what."""

from typing import Any

import pytest

from pycordis.context import Context
from pycordis.errors import CordisError
from pycordis.fiber import FiberState
from pycordis.service import Service
from pycordis.traceable import Tracker


class Hello(Service[Any]):
    def __init__(self, ctx: Context, config: object = None) -> None:
        super().__init__(ctx, "inner")

    def hello(self) -> str:
        return "hello"


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::"
    "keeps caller metadata separate from the service shadow"
)
def test_the_caller_and_the_shadow_of_a_view_are_separate(root: Context) -> None:
    origins: dict[str, Context] = {}

    class Inner(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "inner")
            origins["inner"] = ctx

        def inspect(self) -> dict[str, Context | None]:
            return {"caller": self.cordis_caller, "shadow": self.ctx.shadow}

    class Outer(Service[Any]):
        inject = ["inner"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "outer")
            origins["outer"] = ctx

        def inspect(self) -> dict[str, Context | None]:
            return {**self.ctx["inner"].inspect(), "outer_shadow": self.ctx.shadow}

    root.use(Inner)
    root.use(Outer)
    results: list[dict[str, Context | None]] = []

    root.inject(["outer"], lambda ctx: results.append(ctx["outer"].inspect()))
    results.append(root["outer"].inspect())

    # a read from the root does not collapse the shadow
    for result in results:
        assert result["caller"] is origins["outer"]
        assert result["shadow"] is origins["inner"]
        assert result["outer_shadow"] is origins["outer"]


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::"
    "applies the service inject when entered from root"
)
def test_a_service_without_inject_cannot_read_a_service_when_entered_from_root(
    root: Context,
) -> None:
    class Outer(Service[Any]):  # does not inject `inner`
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "outer")

        def probe(self) -> str:
            return self.ctx["inner"].hello()

    root.use(Hello)
    root.use(Outer)
    message = 'cannot get property "inner" without inject'

    with pytest.raises(CordisError, match=message):
        root["outer"].probe()

    def reader(ctx: Context) -> None:
        with pytest.raises(CordisError, match=message):
            ctx["outer"].probe()

    root.inject(["outer"], reader)


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::resolves dependencies through a captured shadow"
)
def test_an_object_that_captures_a_shadow_context_resolves_at_one_level(
    root: Context,
) -> None:
    class Holder:
        """A plain object that a service hands out, with the context of the service."""

        cordis_tracker = Tracker(property="ctx")

        def __init__(self, ctx: Context) -> None:
            self.ctx = ctx

        def probe(self) -> list[str]:
            return [self.ctx["outer"].tag(), self.ctx["inner"].hello()]

        def depth(self) -> int:
            depth, cursor = 0, self.ctx
            while cursor.shadow is not None:
                depth, cursor = depth + 1, cursor.shadow
            return depth

    class Outer(Service[Any]):
        inject = ["inner"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "outer")

        def tag(self) -> str:
            return "outer"

        def create(self) -> Holder:
            return Holder(self.ctx)

    root.use(Hello)
    root.use(Outer)
    seen: list[Any] = []

    def read(outer: Any) -> None:
        seen.extend([outer.create().probe(), outer.create().depth()])

    read(root["outer"])
    root.inject(["outer"], lambda ctx: read(ctx["outer"]))

    assert seen == [["outer", "hello"], 1] * 2


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::"
    "resolves same-name own provisions at their definition sites"
)
def test_same_name_provisions_resolve_at_their_def_sites(root: Context) -> None:
    first, second = object(), object()

    class Probe(Service[Any]):
        def __init__(self, ctx: Context, tag: str) -> None:
            super().__init__(ctx, "foo")
            self.tag = tag

        def inspect(self) -> list[object]:
            return [self.ctx["foo"].tag, self.ctx.isolates["foo"]]

    class Relay(Service[Any]):
        inject = ["foo"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "relay")

        def inspect(self) -> list[object]:
            return self.ctx["foo"].inspect()

    def provide_both(ctx: Context, config: object) -> None:
        Probe(ctx.isolate("foo", first), "first")
        Probe(ctx.isolate("foo", second), "second")

    root.use(provide_both)
    root.isolate("foo", first).use(Relay)

    # the use site changes; the def sites of the relay and of its probe stay in the
    # first realm
    assert root.isolate("foo", second)["relay"].inspect() == ["first", second]
    assert root.isolate("foo", second)["foo"].inspect() == ["second", second]


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::"
    "keeps unchecked access for services provided on the root ctx"
)
def test_an_object_provided_on_the_root_reads_services_unchecked(
    root: Context,
) -> None:
    class Rooted:
        # provided outside the fiber system, so it can never declare `inject`
        cordis_tracker = Tracker(property="ctx")

        def __init__(self, ctx: Context) -> None:
            self.ctx = ctx

        def probe(self) -> str:
            return self.ctx["inner"].hello()

    root.use(Hello)
    root.provide("rooted", Rooted(root))

    assert root["rooted"].probe() == "hello"


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::exposes the caller without preserving shadow "
    "for noShadow services"
)
def test_a_no_shadow_view_exposes_the_caller_but_no_shadow(root: Context) -> None:
    origins: list[Context] = []

    class Probe:
        cordis_tracker = Tracker(property="ctx", no_shadow=True)

        def __init__(self, ctx: Context) -> None:
            self.ctx = ctx

        def inspect(self) -> dict[str, Context | None]:
            return {"caller": self.cordis_caller, "shadow": self.ctx.shadow}  # type: ignore[attr-defined]

    class Outer(Service[Any]):
        inject = ["probe"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "outer")
            origins.append(ctx)

        def inspect(self) -> dict[str, Context | None]:
            return self.ctx["probe"].inspect()

    root.provide("probe", Probe(root))
    root.use(Outer)
    results: list[dict[str, Context | None]] = []

    root.inject(["outer"], lambda ctx: results.append(ctx["outer"].inspect()))

    assert results[0]["caller"] is origins[0]
    assert results[0]["shadow"] is None


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::exposes the caller to callable services"
)
def test_a_callable_service_exposes_the_caller(root: Context) -> None:
    origins: list[Context] = []

    class Callable(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "callable")

        def cordis_invoke(self) -> Context | None:
            return self.cordis_caller

    class Outer(Service[Any]):
        inject = ["callable"]

        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "outer")
            origins.append(ctx)

        def call(self) -> Context | None:
            return self.ctx["callable"]()

    root.use(Callable)
    root.use(Outer)
    callers: list[Context | None] = []

    root.inject(["outer"], lambda ctx: callers.append(ctx["outer"].call()))

    assert callers == [origins[0]]


@pytest.mark.upstream(
    "shadow.spec.ts::Traceable caller::strips service shadow before creating components"
)
def test_a_component_created_through_a_view_gets_a_context_without_shadow(
    root: Context,
) -> None:
    class Loader(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "loader")

        def load(self, component: Any) -> Any:
            return self.ctx.use(component)

    class Server(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "server")

    injected: list[bool] = []

    def consumer(ctx: Context, config: object) -> None:
        ctx.inject(
            ["server"], lambda ctx: injected.append(isinstance(ctx["server"], Server))
        )

    root.use(Loader)
    fibers: list[Any] = []

    def load_both(ctx: Context) -> None:
        loader = ctx["loader"]
        fibers.extend([loader.load(Server), loader.load(consumer)])

    root.inject(["loader"], load_both)

    assert injected == [True]
    assert [fiber.state for fiber in fibers] == [FiberState.ACTIVE] * 2
    assert [fiber.error for fiber in fibers] == [None, None]
    assert [fiber.parent.shadow for fiber in fibers] == [None, None]
