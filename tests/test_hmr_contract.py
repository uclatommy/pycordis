"""Contract properties of `Hmr` under seeded random schedules.

Each test writes plugin modules, calls `reconcile`, and pumps a random number of steps
of a `FramePumpScheduler` between the calls. The seed is part of every assertion
message.
"""

import random
import textwrap
from typing import Any

import pytest
from conftest import check_well_formed
from hmr_fixtures import STATS, PluginDir

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import Fiber, FiberState
from pycordis.hmr import Hmr, HmrConfig
from pycordis.scheduler import FramePumpScheduler

SEEDS = range(30)
QUIESCENCE_LIMIT = 10_000

# The component installs one effect, and raises when its module says `fail`.
FLAKY = """
    from hmr_fixtures import STATS

    MODE = "{mode}"

    def apply(ctx, config=None):
        STATS["applied"] = MODE

        def install():
            STATS["effects"] = STATS.get("effects", 0) + 1
            return lambda: STATS.update(effects=STATS["effects"] - 1)

        ctx.effect(install)
        if MODE == "fail":
            raise ValueError("flaky")
"""

SIBLING = """
    from hmr_fixtures import STATS

    def sibling(ctx, config=None):
        STATS["sibling_applies"] = STATS.get("sibling_applies", 0) + 1
"""

DEP = "tag = '{tag}'\n"

SERVICE = """
    from dep{parity} import tag

    def apply(ctx, config=None):
        ctx.provide("svc{index}", (tag, "{variant}"))
"""

BROKEN = "def apply(ctx, config=None:\n"


class Host:
    """A root context on a pumped scheduler, with `Hmr` over the plugin directory."""

    def __init__(self, seed: int, plugins: PluginDir) -> None:
        self.seed = seed
        self.plugins = plugins
        self.pump = FramePumpScheduler()
        self.root = Context(self.pump)
        self.hmr_fiber = self.root.use(Hmr, HmrConfig(scope=(str(plugins.path),)))
        self.settle()
        self.hmr: Hmr = self.root.get("hmr")

    def settle(self) -> None:
        steps = self.pump.pump(QUIESCENCE_LIMIT)
        assert steps < QUIESCENCE_LIMIT, f"seed {self.seed}: no quiescence"

    def write(self, name: str, template: str, **values: Any) -> Any:
        return self.plugins.write(
            f"{name}.py", textwrap.dedent(template).format(**values)
        )

    def step(self, rng: random.Random) -> None:
        """Pump a few single steps, and check the registry after each."""
        for _ in range(rng.randrange(4)):
            self.pump.pump(1)
            check_well_formed(self.root, self.seed)


@pytest.fixture
def host_factory(plugins: PluginDir, request: pytest.FixtureRequest) -> Any:
    def make(seed: int) -> Host:
        host = Host(seed, plugins)
        # the disposal runs on the pump, and the next test needs the active place
        request.addfinalizer(lambda: (host.hmr_fiber.dispose(), host.settle()))
        return host

    return make


@pytest.mark.contract("LIFE-FAILURE")
@pytest.mark.parametrize("seed", SEEDS)
def test_a_new_component_that_raises_leaves_its_fiber_failed_and_its_siblings_alone(
    seed: int, host_factory: Any
) -> None:
    rng = random.Random(seed)
    host = host_factory(seed)
    path = host.write("flaky", FLAKY, mode="ok")
    host.write("sibling_mod", SIBLING)
    import flaky  # type: ignore[import-not-found]
    import sibling_mod  # type: ignore[import-not-found]

    fiber: Fiber = host.root.use(flaky.apply)
    sibling = host.root.use(sibling_mod.sibling)
    host.settle()
    pending: list[Deferred[Any]] = []

    for _ in range(12):
        kind = rng.choices(["ok", "fail", "dispose"], [3, 4, 0.1])[0]
        if kind == "dispose":
            fiber.dispose()
        else:
            host.write("flaky", FLAKY, mode=kind)
            pending.append(host.hmr.reconcile(lambda: [path]))
        host.step(rng)
    host.settle()

    assert all(item.done() for item in pending), f"seed {seed}"
    if fiber.uid is None:
        assert STATS.get("effects", 0) == 0, f"seed {seed}"
    elif STATS["applied"] == "fail":
        assert fiber.state == FiberState.FAILED, f"seed {seed}"
        assert isinstance(fiber.error, ValueError), f"seed {seed}"
        assert fiber.get_effects() == [], f"seed {seed}"
        assert STATS["effects"] == 0, f"seed {seed}"
    else:
        assert fiber.state == FiberState.ACTIVE, f"seed {seed}"
        assert STATS["effects"] == 1, f"seed {seed}"
    assert sibling.state == FiberState.ACTIVE, f"seed {seed}"
    assert STATS["sibling_applies"] == 1, f"seed {seed}"


@pytest.mark.contract("REG-WELLFORMED")
@pytest.mark.parametrize("seed", SEEDS)
def test_an_aborted_unit_leaves_every_key_with_the_provider_it_had(
    seed: int, host_factory: Any
) -> None:
    rng = random.Random(seed)
    host = host_factory(seed)
    count = 4
    deps = [host.write(f"dep{p}", DEP, tag="t0") for p in range(2)]
    paths = [
        host.write(f"svc{i}", SERVICE, parity=i % 2, index=i, variant="v0")
        for i in range(count)
    ]
    modules = [__import__(f"svc{i}") for i in range(count)]
    fibers = [host.root.use(module.apply) for module in modules]
    # a transaction with no change records the module graph before the files change
    host.hmr.reconcile(lambda: [])
    host.settle()
    before = [host.root.get(f"svc{i}") for i in range(count)]
    assert before == [("t0", "v0")] * count

    dep_changed = [rng.random() < 0.5 for _ in range(2)]
    rewritten = [rng.random() < 0.5 for _ in range(count)]
    broken = [rewritten[i] and rng.random() < 0.4 for i in range(count)]
    if not any(dep_changed) and not any(rewritten):
        dep_changed[0] = True
    changes: list[Any] = []
    for p in range(2):
        if dep_changed[p]:
            host.write(f"dep{p}", DEP, tag="t1")
            changes.append(deps[p])
    for i in range(count):
        if rewritten[i]:
            if broken[i]:
                paths[i].write_text(BROKEN)
            else:
                host.write(f"svc{i}", SERVICE, parity=i % 2, index=i, variant="v1")
            changes.append(paths[i])

    result = host.hmr.reconcile(lambda: changes)
    while not result.done():
        host.step(rng)
        host.pump.pump(1)
        check_well_formed(host.root, seed)
    host.settle()
    outcome = result.result()

    for i in range(count):
        parity = i % 2
        members = (
            [j for j in range(count) if j % 2 == parity] if dep_changed[parity] else [i]
        )
        aborted = any(broken[j] for j in members)
        tag = "t1" if dep_changed[parity] else "t0"
        variant = "v1" if rewritten[i] else "v0"
        untouched = not dep_changed[parity] and not rewritten[i]
        expected = before[i] if aborted or untouched else (tag, variant)
        assert host.root.get(f"svc{i}") == expected, f"seed {seed}: svc{i}"
        assert fibers[i].state == FiberState.ACTIVE, f"seed {seed}: svc{i}"
    assert bool(outcome.aborted) == any(broken[i] for i in range(count)), f"seed {seed}"
    check_well_formed(host.root, seed)

    # a later transaction retries the aborted units with the repaired files
    for i in range(count):
        host.write(f"svc{i}", SERVICE, parity=i % 2, index=i, variant="v2")
    retry = host.hmr.reconcile(lambda: [*deps, *paths])
    host.settle()
    assert not retry.result().aborted, f"seed {seed}"
    tag = "t1"
    for i in range(count):
        want = (tag if dep_changed[i % 2] else "t0", "v2")
        assert host.root.get(f"svc{i}") == want, f"seed {seed}: svc{i}"
    check_well_formed(host.root, seed)
