"""`Hmr` with the loader: the entries of a tree keep their fibers across a reload."""

import logging
import textwrap
from pathlib import Path
from typing import Any

import pytest
from hmr_fixtures import STATS, PluginDir

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import Fiber, FiberState
from pycordis.hmr import Hmr, HmrConfig, ReloadResult
from pycordis.loader import Entry, Loader

# Each template has the token `VERSION`, which `Setup.write` replaces.

DEP = "version = 'VERSION'\n"

NESTED = """
    from hmr_fixtures import STATS
    from tree_dep import version

    def apply(ctx, config=None):
        STATS["nested_applied"] = STATS.get("nested_applied", 0) + 1
        ctx.on("hmr-test/get-nested", lambda: version)
        ctx.effect(lambda: lambda: STATS.update(
            nested_disposed=STATS.get("nested_disposed", 0) + 1))
"""

# A host that holds a tree: its fiber owns the entries of the tree, so a reload of the
# host builds them again.
TREE = """
    from hmr_fixtures import STATS
    from pycordis.loader import EntryTree
    from tree_dep import version

    class Tree(EntryTree):
        inject = ["loader"]

        def __init__(self, ctx, config=None):
            super().__init__(ctx)

        def commit(self, change):
            pass

        async def cordis_init(self):
            self.ctx.on("hmr-test/get-tree", lambda: version)
            yield self.root.stop
            await self.root.update([{"id": "nested", "name": "component_nested"}])
            stall = STATS.get("stall")
            if stall is not None:
                await stall
"""

# A component that owns an exclusive resource: it releases the resource when
# `STATS["drain"]` completes, and the next instance must not start before that.
SLOW = """
    from hmr_fixtures import STATS
    from drain_dep import version

    def apply(ctx, config=None):
        if STATS.get("slow_held"):
            STATS["slow_overlaps"] = STATS.get("slow_overlaps", 0) + 1
        STATS["slow_held"] = True
        ctx.on("hmr-test/get-slow", lambda: version)

        def unload():
            drain = STATS.get("drain")
            if drain is None:
                STATS["slow_held"] = False
                return None
            drain.add_done_callback(lambda _: STATS.update(slow_held=False))
            return drain

        return unload
"""

FAST = """
    from hmr_fixtures import STATS
    from drain_dep import version

    def apply(ctx, config=None):
        STATS["fast_applied"] = STATS.get("fast_applied", 0) + 1
        STATS["fast_saw_slow_held"] = STATS.get("slow_held")
        ctx.on("hmr-test/get-fast", lambda: version)
"""

# Two entries use this module, so its component has two fibers. Each answers for its
# own label.
MULTI = """
    from hmr_fixtures import STATS

    version = "VERSION"

    def apply(ctx, config):
        STATS.setdefault("multi_applies", []).append(config["label"] + ":" + version)
        ctx.on(
            "hmr-test/get-multi",
            lambda label: version if label == config["label"] else None,
        )
"""

# An entry whose unload a test can hold: the disposer waits for `STATS["hold"]` and
# tells the test through `STATS["entered"]` when the unload starts.
GATED = """
    from hmr_fixtures import STATS

    value = "VERSION"

    def apply(ctx, config=None):
        STATS["shared"] = apply
        STATS["applies"] = STATS.get("applies", 0) + 1
        STATS.setdefault("fibers", []).append(ctx.fiber)
        ctx.on("hmr-test/get-value", lambda: value)

        def unload():
            entered = STATS.get("entered")
            if entered is not None and not entered.done():
                entered.resolve(None)
            return STATS.get("hold")

        return unload
"""

# A host that does not import the module above, so a change there does not reload the
# host. It uses the function that the module stored, as a host that reuses a component.
HOST = """
    from hmr_fixtures import STATS

    def apply(ctx, config=None):
        ctx.use(STATS["shared"])
"""


class Setup:
    """A root context with `Loader` and `Hmr` over a directory of plugin modules."""

    def __init__(self, ctx: Context, plugins: PluginDir) -> None:
        self.ctx = ctx
        self.plugins = plugins
        self.hmr_fiber = ctx.use(Hmr, HmrConfig(scope=(str(plugins.path),)))
        ctx.use(Loader)
        self.loader: Loader = ctx.get("loader")
        self.hmr: Hmr = ctx.get("hmr")

    def write(self, name: str, template: str, version: str = "v1") -> Path:
        text = textwrap.dedent(template).replace("VERSION", version)
        return self.plugins.write(f"{name}.py", text)

    def rewrite(self, path: Path, template: str, version: str) -> None:
        path.write_text(textwrap.dedent(template).replace("VERSION", version))

    def reload(self, path: Path) -> Deferred[ReloadResult]:
        return self.hmr.reconcile(lambda: [path])

    def entry(self, identifier: str) -> Entry:
        return self.loader.resolve(identifier)

    def inner(self, identifier: str) -> Fiber:
        fiber = self.entry(identifier).inner_fiber
        assert fiber is not None
        return fiber

    def value(self) -> Any:
        return self.ctx.bail("hmr-test/get-value")


@pytest.fixture
def setup(root: Context, plugins: PluginDir, request: pytest.FixtureRequest) -> Setup:
    """Start `Hmr` and the loader; the fixture ends `Hmr` with the test."""
    environment = Setup(root, plugins)
    request.addfinalizer(environment.hmr_fiber.dispose)
    return environment


@pytest.fixture
def gated(setup: Setup) -> Setup:
    """A `Setup` with the entry `live`, which runs the gated module."""
    setup.write("component_gated", GATED, "initial")
    setup.loader.read([{"id": "live", "name": "component_gated"}]).result()
    assert setup.value() == "initial"
    return setup


def hold_unload() -> Deferred[None]:
    """Hold the unload of the gated component. Return the signal that it started."""
    STATS["hold"] = Deferred[None]()
    STATS["entered"] = Deferred[None]()
    return STATS["entered"]


def release_unload() -> None:
    STATS["hold"].resolve(None)


def live_fibers() -> list[Fiber]:
    # a fiber that moves runs `apply` again, so the list holds it once for each run
    unique = {id(fiber): fiber for fiber in STATS.get("fibers", [])}
    return [fiber for fiber in unique.values() if fiber.uid is not None]


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::nested entry tree::should keep a single nested entry when its host reloads too"  # noqa: E501
)
def test_the_entry_of_a_tree_that_reloads_with_its_host_runs_once(
    setup: Setup,
) -> None:
    dep = setup.write("tree_dep", DEP, "tree-v1")
    setup.write("component_nested", NESTED)
    setup.write("component_tree", TREE)
    setup.loader.read([{"id": "tree", "name": "component_tree:Tree"}]).result()
    assert setup.ctx.bail("hmr-test/get-nested") == "tree-v1"

    setup.rewrite(dep, DEP, "tree-v2")
    result = setup.reload(dep).result()

    assert FiberState.FAILED not in [fiber.state for fiber in result.settled]
    assert setup.ctx.bail("hmr-test/get-tree") == "tree-v2"
    assert setup.ctx.bail("hmr-test/get-nested") == "tree-v2"
    # the host builds its tree again, which disposes the nested entry that moved
    assert STATS["nested_applied"] - STATS["nested_disposed"] == 1


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::nested entry tree::should keep a single nested entry across a batch queued behind a stalled host"  # noqa: E501
)
def test_a_transaction_that_waits_for_a_stalled_host_keeps_one_nested_entry(
    setup: Setup,
) -> None:
    dep = setup.write("tree_dep", DEP, "tree-v1")
    setup.write("component_nested", NESTED)
    setup.write("component_tree", TREE)
    setup.loader.read([{"id": "tree", "name": "component_tree:Tree"}]).result()

    stall = STATS["stall"] = Deferred[None]()
    setup.rewrite(dep, DEP, "tree-v2")
    first = setup.reload(dep)
    assert setup.ctx.bail("hmr-test/get-nested") == "tree-v2"
    assert not first.done()

    # the second transaction waits for the first, which waits for the host
    STATS["stall"] = None
    setup.rewrite(dep, DEP, "tree-v3")
    second = setup.reload(dep)
    assert not second.done()
    assert setup.ctx.bail("hmr-test/get-nested") == "tree-v2"

    stall.resolve(None)
    results = [first.result(), second.result()]

    settled = [fiber for result in results for fiber in result.settled]
    assert FiberState.FAILED not in [fiber.state for fiber in settled]
    assert setup.ctx.bail("hmr-test/get-tree") == "tree-v3"
    assert setup.ctx.bail("hmr-test/get-nested") == "tree-v3"
    assert STATS["nested_applied"] - STATS["nested_disposed"] == 1


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::resource handoff on reload::should not start the new instance before the old one released"  # noqa: E501
)
@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::resource handoff on reload::should not delay an unrelated component behind a slow one"  # noqa: E501
)
def test_a_slow_unload_delays_only_its_own_component(setup: Setup) -> None:
    dep = setup.write("drain_dep", DEP, "drain-v1")
    setup.write("component_slow", SLOW)
    setup.write("component_fast", FAST)
    setup.loader.read(
        [
            {"id": "test-slow", "name": "component_slow"},
            {"id": "test-fast", "name": "component_fast"},
        ]
    ).result()
    drain = STATS["drain"] = Deferred[None]()

    setup.rewrite(dep, DEP, "drain-v3")
    reloaded = setup.reload(dep)

    # the fast component is back while the slow one still releases its resource
    assert setup.ctx.bail("hmr-test/get-fast") == "drain-v3"
    assert STATS["fast_saw_slow_held"] is True
    assert STATS.get("slow_overlaps", 0) == 0
    assert setup.ctx.bail("hmr-test/get-slow") is None
    assert not reloaded.done()

    drain.resolve(None)
    reloaded.result()

    assert setup.ctx.bail("hmr-test/get-slow") == "drain-v3"
    assert STATS.get("slow_overlaps", 0) == 0


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::component with multiple entries::should rebuild every fiber, each keeping its own config"  # noqa: E501
)
def test_every_entry_of_a_component_moves_with_its_own_config(setup: Setup) -> None:
    path = setup.write("component_multi", MULTI)
    setup.loader.read(
        [
            {"id": "multi-a", "name": "component_multi", "config": {"label": "a"}},
            {"id": "multi-b", "name": "component_multi", "config": {"label": "b"}},
        ]
    ).result()
    assert setup.ctx.bail("hmr-test/get-multi", "a") == "v1"
    assert setup.ctx.bail("hmr-test/get-multi", "b") == "v1"

    STATS["multi_applies"] = []
    setup.rewrite(path, MULTI, "v2")
    result = setup.reload(path).result()

    assert len(result.settled) == 2
    assert sorted(STATS["multi_applies"]) == ["a:v2", "b:v2"]


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::entry changed during unload::does not rebuild an entry disabled during unload"  # noqa: E501
)
@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::entry changed during unload::does not rebuild an entry removed during unload"  # noqa: E501
)
@pytest.mark.parametrize("change", ["disable", "remove"])
def test_an_entry_that_leaves_during_its_unload_is_not_loaded_again(
    gated: Setup, change: str
) -> None:
    entry = gated.entry("live")
    previous = gated.inner("live")
    inside = hold_unload()
    gated.rewrite(gated.plugins.path / "component_gated.py", GATED, "new")
    STATS["applies"] = 0
    reloaded = gated.reload(gated.plugins.path / "component_gated.py")
    assert inside.done()

    if change == "disable":
        gated.loader.update("live", {"disabled": True}).result()
    else:
        gated.loader.remove("live")
    release_unload()
    reloaded.result()

    assert entry.inner_fiber is previous
    assert previous.uid is None
    assert gated.value() is None
    assert STATS["applies"] == 0
    assert live_fibers() == []
    if change == "disable":
        assert entry.disabled


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::entry changed during unload::waits for a second re-enable init before deciding whether to rebuild"  # noqa: E501
)
def test_an_entry_that_is_enabled_again_during_its_unload_loads_once_for_each_enable(
    gated: Setup,
) -> None:
    path = gated.plugins.path / "component_gated.py"
    previous = gated.inner("live")
    inside = hold_unload()
    gated.rewrite(path, GATED, "second-init")
    STATS["applies"] = 0
    reloaded = gated.reload(path)
    assert inside.done()

    gated.loader.update("live", {"disabled": True})
    updating = gated.loader.update("live", {"disabled": None})
    release_unload()
    reloaded.result()
    updating.result()
    gated.loader.wait().result()

    # the entry loads the new module once; the reload loads nothing
    assert previous.uid is None
    assert gated.value() == "second-init"
    assert STATS["applies"] == 1
    assert live_fibers() == [gated.inner("live")]

    gated.loader.update("live", {"disabled": True}).result()
    gated.loader.update("live", {"disabled": None}).result()

    assert STATS["applies"] == 2
    assert live_fibers() == [gated.inner("live")]


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::entry changed during unload::rebuilds a child of an active host that reused the component"  # noqa: E501
)
def test_a_fiber_of_a_host_that_reuses_the_component_moves_with_the_entry(
    gated: Setup,
) -> None:
    path = gated.plugins.path / "component_gated.py"
    gated.write("component_host", HOST)
    gated.loader.create({"id": "host", "name": "component_host"}).result()
    previous, host_root = gated.inner("live"), gated.inner("host")
    assert len(live_fibers()) == 2
    applies = STATS["applies"]

    gated.rewrite(path, GATED, "from-host")
    result = gated.reload(path).result()

    assert len(result.settled) == 2
    assert gated.value() == "from-host"
    # the entry and the child of the host run the new code in place
    assert STATS["applies"] - applies == 2
    assert gated.inner("live") is previous
    assert gated.inner("host") is host_root
    assert len(live_fibers()) == 2

    gated.loader.update("host", {"disabled": True}).result()
    assert host_root.uid is None
    assert live_fibers() == [previous]


@pytest.mark.upstream(
    "hmr/index.spec.ts::HMR::entry changed during unload::skips a child whose host is replaced during unload"  # noqa: E501
)
def test_a_child_of_a_host_that_restarts_during_the_unload_runs_in_the_new_host(
    gated: Setup, caplog: pytest.LogCaptureFixture
) -> None:
    path = gated.plugins.path / "component_gated.py"
    gated.write("component_host", HOST)
    gated.loader.create({"id": "host", "name": "component_host"}).result()
    host_root = gated.inner("host")
    assert len(live_fibers()) == 2

    inside = hold_unload()
    gated.rewrite(path, GATED, "host-replaced")
    reloaded = gated.reload(path)
    assert inside.done()
    with caplog.at_level(logging.WARNING):
        gated.loader.update("host", {"disabled": True})
        gated.loader.update("host", {"disabled": None})
        release_unload()
        result = reloaded.result()
        gated.loader.wait().result()

    assert [record.getMessage() for record in caplog.records] == []
    assert FiberState.FAILED not in [fiber.state for fiber in result.settled]
    assert host_root.uid is None
    assert gated.inner("host").uid is not None
    # the entry, and the child that the new host mounted
    assert len(live_fibers()) == 2
    assert gated.value() == "host-replaced"


ENTRY = """
    from hmr_fixtures import STATS

    def apply(ctx, config=None):
        ctx.on("reconcile/entry", lambda: "VERSION")
        ctx.effect(lambda: lambda: STATS.update(disposed=True))
"""


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::replaces an entry component in place"
)
def test_an_entry_keeps_its_fiber_when_its_component_reloads(setup: Setup) -> None:
    path = setup.write("entry_mod", ENTRY)
    setup.loader.read([{"id": "entry", "name": "entry_mod"}]).result()
    inner = setup.inner("entry")
    assert setup.ctx.bail("reconcile/entry") == "v1"

    setup.rewrite(path, ENTRY, "v2")
    result = setup.reload(path).result()

    assert result.settled == [inner]
    assert (result.settled[0].state, result.aborted) == (FiberState.ACTIVE, [])
    assert STATS["disposed"] is True
    assert setup.inner("entry") is inner
    assert not setup.entry("entry").disabled
    assert setup.ctx.bail("reconcile/entry") == "v2"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::replaces the inner fiber of an entry with an injection scope in place"  # noqa: E501
)
def test_an_entry_with_an_inject_option_keeps_its_outer_and_inner_fibers(
    setup: Setup,
) -> None:
    setup.ctx.provide("reconcileScope", {})
    path = setup.write("entry_mod", ENTRY)
    setup.loader.read(
        [{"id": "entry", "name": "entry_mod", "inject": ["reconcileScope"]}]
    ).result()
    outer, inner = setup.entry("entry").outer_fiber, setup.inner("entry")
    assert outer is not None
    assert setup.ctx.bail("reconcile/entry") == "v1"

    setup.rewrite(path, ENTRY, "v2")
    result = setup.reload(path).result()

    assert result.settled == [inner]
    assert setup.entry("entry").outer_fiber is outer
    assert setup.inner("entry") is inner
    assert setup.ctx.bail("reconcile/entry") == "v2"


DISABLED = """
    from disabled_dep import value

    def apply(ctx, config=None):
        ctx.on("reconcile/disabled", lambda: value)
"""


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::refreshes a disabled component for when it is enabled again"  # noqa: E501
)
def test_a_disabled_entry_loads_the_new_code_when_it_is_enabled_again(
    setup: Setup,
) -> None:
    dep = setup.write("disabled_dep", "value = 'VERSION'\n")
    setup.write("disabled_mod", DISABLED)
    setup.loader.read([{"id": "off", "name": "disabled_mod"}]).result()
    setup.loader.update("off", {"disabled": True}).result()

    setup.rewrite(dep, "value = 'VERSION'\n", "v2")
    setup.reload(dep).result()
    assert setup.entry("off").disabled
    setup.loader.update("off", {"disabled": None}).result()

    assert setup.ctx.bail("reconcile/disabled") == "v2"
