"""Provider disposal waits for the dependents that read the provider's services."""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import Fiber
from pycordis.scheduler import spawn
from pycordis.service import Service


class Fixture:
    """A provider of `foo` that records when it closes and when a consumer reads it."""

    def __init__(self, root: Context) -> None:
        self.root = root
        self.timeline: list[str] = []
        self.gate = Deferred[None]()
        self.closed = False
        fixture = self

        class Provider(Service[Any]):
            def __init__(self, ctx: Context, config: object = None) -> None:
                super().__init__(ctx, "foo")

            def cordis_init(self) -> Iterator[Callable[[], None]]:
                fixture.closed = False

                def close() -> None:
                    fixture.closed = True
                    fixture.timeline.append("provider closed")

                yield close

            def read(self) -> None:
                state = "closed" if fixture.closed else "open"
                fixture.timeline.append(f"consumer read: {state}")

        self.Provider = Provider

    def after(
        self, gate: Deferred[None], then: Callable[[], None]
    ) -> Callable[[], Any]:
        """Return a disposer that waits for `gate`, and then runs `then`."""

        async def run() -> None:
            await gate
            then()

        return lambda: spawn(self.root.scheduler, run())


@pytest.fixture
def fx(root: Context) -> Fixture:
    return Fixture(root)


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::releases an injected binding after "
    "providing the same name in another realm (consumer)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::releases an injected binding after "
    "providing the same name in another realm (provider)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::releases an injected binding after "
    "providing the same name in another realm (root)"
)
@pytest.mark.parametrize("action", ["consumer", "provider", "root"])
def test_a_binding_is_released_after_the_same_name_is_provided_in_another_realm(
    root: Context, action: str
) -> None:
    provider = root.use(lambda ctx, config: ctx.provide("foo", {"value": 1}))

    def consume(ctx: Context) -> None:
        ctx.isolate("foo").provide("foo", {"outer": ctx["foo"]})

    consumer = root.inject(["foo"], consume)
    if action == "root":
        task = root.fiber.dispose()
    elif action == "provider":
        task = provider.dispose()
    else:
        consumer.dispose()
        task = provider.dispose()

    assert task.done()
    task.result()
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::waits for a disposing consumer that "
    "has already left the registry"
)
def test_a_withdrawal_waits_for_a_consumer_that_is_disposing(
    root: Context, fx: Fixture
) -> None:
    provider = root.use(lambda ctx, config: None)
    withdraw = provider.ctx.provide("foo", {})
    consumer = root.inject(
        ["foo"], lambda ctx: fx.after(fx.gate, lambda: fx.timeline.append("consumer"))
    )
    disposing = consumer.dispose()
    withdrawing = withdraw()
    assert withdrawing is not None
    withdrawing.add_done_callback(lambda _: fx.timeline.append("withdraw completed"))

    assert not withdrawing.done()
    fx.gate.resolve(None)

    assert disposing.done() and withdrawing.done()
    assert fx.timeline == ["consumer", "withdraw completed"]
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::withdraws one binding without "
    "waiting for another binding from the same provider"
)
def test_a_withdrawal_does_not_wait_for_the_dependents_of_another_binding(
    root: Context, fx: Fixture
) -> None:
    provider = root.use(lambda ctx, config: None)
    withdraw = provider.ctx.provide("foo", {})
    provider.ctx.provide("bar", {})
    first = root.inject(["foo"], lambda ctx: None)
    second = root.inject(["bar"], lambda ctx: fx.after(fx.gate, lambda: None))
    disposing = second.dispose()

    withdrawing = withdraw()

    assert withdrawing is not None and withdrawing.done()
    assert not disposing.done()
    fx.gate.resolve(None)
    assert disposing.done()
    first.dispose().result()
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains resources during consumer "
    "cleanup (provider)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains resources during consumer "
    "cleanup (root)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains resources during consumer "
    "cleanup (provider first)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains resources during consumer "
    "cleanup (consumer first)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains resources during consumer "
    "cleanup (restart)"
)
@pytest.mark.parametrize(
    "action", ["provider", "root", "provider first", "consumer first", "restart"]
)
def test_a_provider_stays_open_while_a_consumer_cleans_up(
    root: Context, fx: Fixture, action: str
) -> None:
    provider = root.use(fx.Provider)

    def consume(ctx: Context) -> None:
        ctx.effect(lambda: fx.after(fx.gate, lambda: ctx["foo"].read()))

    consumer = root.inject(["foo"], consume)
    if action == "root":
        task = root.fiber.dispose()
    elif action == "restart":
        task = provider.restart()
    elif action == "consumer first":
        consumer.dispose()
        task = provider.dispose()
    elif action == "provider first":
        task = provider.dispose()
        consumer.dispose()
    else:
        task = provider.dispose()

    assert not fx.closed
    fx.gate.resolve(None)

    task.result()
    assert fx.timeline == ["consumer read: open", "provider closed"]
    if action == "restart":
        consumer.wait().result()
        assert consumer.ctx["foo"] is not None
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains transitive dependencies "
    "while a consumer providing another service drains"
)
def test_a_provider_stays_open_while_a_consumer_that_provides_drains(
    root: Context, fx: Fixture
) -> None:
    def owner_apply(ctx: Context, config: object) -> None:
        ctx.use(fx.Provider)

        def middle(ctx: Context) -> Callable[[], None]:
            ctx.provide("bar", {})
            return lambda: ctx["foo"].read()

        ctx.inject(["foo"], middle)

        def last(ctx: Context) -> Callable[[], Any]:
            fx.timeline.append("consumer draining")
            return fx.after(fx.gate, lambda: fx.timeline.append("consumer finished"))

        ctx.inject(["bar"], last)

    owner = root.use(owner_apply)
    task = owner.dispose()

    assert not fx.closed
    fx.gate.resolve(None)

    task.result()
    assert fx.timeline == [
        "consumer draining",
        "consumer finished",
        "consumer read: open",
        "provider closed",
    ]


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains dependencies through "
    "in-flight setup and rollback (failure: false)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains dependencies through "
    "in-flight setup and rollback (failure: true)"
)
@pytest.mark.parametrize("fail", [False, True])
def test_a_provider_stays_open_during_an_unfinished_setup_and_its_rollback(
    root: Context, fx: Fixture, caplog: pytest.LogCaptureFixture, fail: bool
) -> None:
    provider = root.use(fx.Provider)

    async def consume(ctx: Context) -> Any:
        fx.timeline.append("setup started")

        def rollback() -> None:
            ctx["foo"].read()
            fx.timeline.append("setup rolled back")

        yield rollback
        await fx.gate
        ctx["foo"].read()
        if fail:
            raise RuntimeError("setup failed")

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        consumer = root.inject(["foo"], consume)
        task = provider.dispose()
        assert not fx.closed
        fx.gate.resolve(None)
        task.result()
        consumer.dispose().result()

    assert fx.timeline == [
        "setup started",
        "consumer read: open",
        "consumer read: open",
        "setup rolled back",
        "provider closed",
    ]
    logged = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(logged) == (1 if fail else 0)


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::releases dependencies after a "
    "cleanup error"
)
def test_dependencies_are_released_after_a_cleanup_error(
    root: Context, fx: Fixture, caplog: pytest.LogCaptureFixture
) -> None:
    error = RuntimeError("cleanup failed")
    root.use(fx.Provider)

    def consume(ctx: Context) -> Callable[[], None]:
        def cleanup() -> None:
            ctx["foo"].read()
            raise error

        return cleanup

    root.inject(["foo"], consume)

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        root.fiber.dispose().result()

    assert fx.timeline == ["consumer read: open", "provider closed"]
    assert [r.exc_info[1] for r in caplog.records if r.exc_info] == [error]


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::keeps provider listeners alive "
    "through the final consumer event"
)
def test_a_provider_keeps_its_listeners_through_the_last_event_of_a_consumer(
    root: Context, fx: Fixture
) -> None:
    received: list[str] = []

    def provider(ctx: Context, config: object) -> None:
        ctx.provide("foo", {})
        ctx.on("custom-event", lambda: received.append("event"))

    root.use(provider)
    root.inject(
        ["foo"],
        lambda ctx: fx.after(fx.gate, lambda: ctx.emit("custom-event")),
    )

    task = root.fiber.dispose()
    fx.gate.resolve(None)

    task.result()
    assert received == ["event"]


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::allows unrelated providers and "
    "sibling effects to clean up concurrently"
)
def test_unrelated_providers_and_sibling_effects_clean_up_while_a_consumer_drains(
    root: Context, fx: Fixture
) -> None:
    calls: list[str] = []
    root.use(fx.Provider)
    root.use(
        lambda ctx, config: (
            ctx.provide("bar", {}),
            lambda: calls.append("independent"),
        )[1]
    )

    def consume(ctx: Context) -> None:
        ctx.effect(lambda: fx.after(fx.gate, lambda: ctx["foo"].read()))
        ctx.effect(lambda: lambda: calls.append("sibling"))

    root.inject(["foo"], consume)

    task = root.fiber.dispose()

    assert (fx.closed, calls) == (False, ["sibling", "independent"])
    fx.gate.resolve(None)
    task.result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::supports composed provider effects "
    "(owner shutdown: false)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::supports composed provider effects "
    "(owner shutdown: true)"
)
@pytest.mark.parametrize("owner_shutdown", [False, True])
def test_a_provider_made_of_composed_effects_stays_open_while_a_consumer_drains(
    root: Context, fx: Fixture, owner_shutdown: bool
) -> None:
    fibers: dict[str, Fiber] = {}

    def provider_apply(ctx: Context, config: object) -> None:
        def setup() -> Iterator[Any]:
            def close() -> None:
                fx.closed = True
                fx.timeline.append("provider closed")

            yield ctx.effect(lambda: close)
            state = fx
            yield ctx.provide(
                "foo",
                type(
                    "Foo",
                    (),
                    {
                        "read": staticmethod(
                            lambda: state.timeline.append(
                                f"consumer read: {'closed' if state.closed else 'open'}"
                            )
                        )
                    },
                )(),
            )

        ctx.effect(setup)

    def owner_apply(ctx: Context, config: object) -> None:
        fibers["provider"] = ctx.use(provider_apply)
        fibers["consumer"] = ctx.inject(
            ["foo"], lambda ctx: fx.after(fx.gate, lambda: ctx["foo"].read())
        )

    owner = root.use(owner_apply)
    task = owner.dispose() if owner_shutdown else fibers["provider"].dispose()

    assert not fx.closed
    fx.gate.resolve(None)

    task.result()
    assert fx.timeline == ["consumer read: open", "provider closed"]
    owner.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::keeps an inherited service "
    "accessible to a child in a composed effect"
)
def test_a_child_reads_an_inherited_service_while_a_composed_effect_ends_it(
    root: Context,
) -> None:
    reads: list[str] = []
    fibers: list[Fiber] = []

    def owner_apply(ctx: Context, config: object) -> None:
        ctx.provide(
            "foo", type("Foo", (), {"read": lambda self: reads.append("read")})()
        )

        def setup() -> Iterator[Any]:
            child = ctx.use(lambda ctx, config: lambda: ctx["foo"].read())
            fibers.append(child)
            yield child.dispose

        ctx.effect(setup)

    owner = root.use(owner_apply)
    owner.dispose().result()

    assert reads == ["read"]


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::retains an ancestor injection until "
    "its child finishes cleanup"
)
def test_an_injected_service_stays_open_until_a_child_of_the_consumer_ends(
    root: Context, fx: Fixture
) -> None:
    root.use(fx.Provider)

    def consume(ctx: Context) -> None:
        ctx.use(lambda ctx, config: fx.after(fx.gate, lambda: ctx["foo"].read()))

    root.inject(["foo"], consume)

    task = root.fiber.dispose()

    assert not fx.closed
    fx.gate.resolve(None)
    task.result()
    assert fx.timeline == ["consumer read: open", "provider closed"]


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::waits for a child that explicitly "
    "injects its parent service"
)
def test_a_provider_waits_for_its_own_child_that_injects_the_service(
    root: Context, fx: Fixture
) -> None:
    provider = root.use(fx.Provider)
    provider.ctx.inject(
        ["foo"], lambda ctx: fx.after(fx.gate, lambda: ctx["foo"].read())
    )

    task = provider.dispose()

    assert not fx.closed
    fx.gate.resolve(None)
    task.result()
    assert fx.timeline == ["consumer read: open", "provider closed"]


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::tracks same-name services in "
    "separate isolation realms (root shutdown: false)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::tracks same-name services in "
    "separate isolation realms (root shutdown: true)"
)
@pytest.mark.parametrize("root_shutdown", [False, True])
def test_the_dependents_of_one_realm_hold_back_the_provider_of_that_realm(
    root: Context, fx: Fixture, root_shutdown: bool
) -> None:
    first, second = object(), object()
    reads: list[str] = []
    closed: list[str] = []

    def provider_apply(ctx: Context, config: object) -> Callable[[], None]:
        ctx.isolate("foo", first).provide(
            "foo", type("Foo", (), {"read": lambda self: reads.append("read")})()
        )
        ctx.isolate("foo", second).provide("foo", {})
        return lambda: closed.append("closed")

    provider = root.use(provider_apply)
    draining: list[str] = []

    def consume(ctx: Context) -> Callable[[], Any]:
        draining.append("draining")
        return fx.after(fx.gate, lambda: ctx["foo"].read())

    consumer = root.isolate("foo", first).inject(["foo"], consume)
    task = root.fiber.dispose() if root_shutdown else provider.dispose()

    assert closed == [] and draining == ["draining"]
    fx.gate.resolve(None)
    consumer.dispose().result()
    task.result()
    assert (reads, closed) == (["read"], ["closed"])
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::withdraws only the old binding "
    "without waiting for the next load"
)
def test_a_withdrawal_does_not_wait_for_the_next_load_of_the_consumer(
    root: Context,
) -> None:
    old_cleanup = Deferred[None]()
    new_setup = Deferred[None]()
    versions: list[int] = []
    provider = root.use(lambda ctx, config: None)
    withdraw = provider.ctx.provide("foo", {"version": 1})

    async def consume(ctx: Context) -> Callable[[], Any]:
        version = ctx["foo"]["version"]
        versions.append(version)
        if version == 2:
            await new_setup

        async def cleanup() -> None:
            await old_cleanup
            versions.append(ctx["foo"]["version"])

        return lambda: spawn(root.scheduler, cleanup())

    consumer = root.inject(["foo"], consume)
    withdrawal = withdraw()
    assert withdrawal is not None
    provider.ctx.provide("foo", {"version": 2})
    old_cleanup.resolve(None)

    assert withdrawal.done()
    assert provider.ctx["foo"]["version"] == 2
    new_setup.resolve(None)
    consumer.wait().result()
    assert versions == [1, 1, 2]
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::waits for current and withdrawn "
    "bindings during provider disposal (withdrawn first: false)"
)
@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::waits for current and withdrawn "
    "bindings during provider disposal (withdrawn first: true)"
)
@pytest.mark.parametrize("withdrawn_first", [False, True])
def test_a_provider_waits_for_the_dependents_of_current_and_withdrawn_bindings(
    root: Context, fx: Fixture, withdrawn_first: bool
) -> None:
    current = Deferred[None]()
    provider = root.use(fx.Provider)
    withdraw = provider.ctx.provide("bar", {})
    root.inject(["foo"], lambda ctx: fx.after(current, lambda: ctx["foo"].read()))
    root.inject(
        ["bar"],
        lambda ctx: fx.after(fx.gate, lambda: fx.timeline.append("consumer finished")),
    )
    withdrawal = withdraw()
    assert withdrawal is not None
    provider.ctx.provide("bar", {})
    task = provider.dispose()
    first, second = (fx.gate, current) if withdrawn_first else (current, fx.gate)

    assert not fx.closed
    first.resolve(None)
    assert not fx.closed
    second.resolve(None)

    assert withdrawal.done()
    task.result()
    assert fx.timeline == (
        ["consumer finished", "consumer read: open", "provider closed"]
        if withdrawn_first
        else ["consumer read: open", "consumer finished", "provider closed"]
    )
    root.fiber.dispose().result()


@pytest.mark.upstream(
    "service-dispose.spec.ts::Service disposal::replaces a binding from the same "
    "provider during consumer initialization"
)
def test_a_binding_is_replaced_while_a_consumer_initializes(root: Context) -> None:
    setup = Deferred[None]()
    versions: list[int] = []
    withdraw = root.provide("foo", {"version": 1})

    async def consume(ctx: Context) -> Callable[[], None]:
        versions.append(ctx["foo"]["version"])
        await setup
        return lambda: versions.append(-ctx["foo"]["version"])

    consumer = root.inject(["foo"], consume)
    withdrawal = withdraw()
    assert withdrawal is not None
    root.provide("foo", {"version": 2})
    setup.resolve(None)
    consumer.wait().result()

    assert withdrawal.done()
    assert consumer.ctx["foo"]["version"] == 2
    consumer.dispose().result()
    assert versions == [1, -1, 2, -2]
    root.fiber.dispose().result()
