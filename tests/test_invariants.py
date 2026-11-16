"""Contract properties under seeded random schedules.

Each test builds a small pool of components with dependencies, applies a random list of
`use`, `dispose`, and `replace` calls to a `FramePumpScheduler` context, and pumps a
random number of steps between the calls. The seed is part of every assertion message.
"""

import itertools
import random
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import hook_snapshot

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import Fiber, FiberState
from pycordis.scheduler import FramePumpScheduler

SEEDS = range(30)
OPS = 30
QUIESCENCE_LIMIT = 10_000
EVENT = "custom-event"

type Op = tuple[str, int]
type Token = tuple[str, int]


@dataclass
class Probe:
    """What the components record about their own setup and teardown."""

    installed: dict[object, tuple[Token, ...]] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    reads: dict[int, set[tuple[Token, ...]]] = field(default_factory=dict)
    serial: itertools.count = field(default_factory=itertools.count)

    def uninstall(self, marker: object) -> None:
        del self.installed[marker]


@dataclass
class Slot:
    """One component of the pool, and the calls that the model made for it."""

    name: str
    variants: list[Any]
    live: bool = False
    replaced: bool = False
    fiber: Fiber | None = None
    config: Any = None
    provides: bool = False

    @property
    def current(self) -> Any:
        return self.variants[1 if self.replaced else 0]


def provider(
    probe: Probe, name: str, key: str, deps: tuple[str, ...] = ()
) -> SimpleNamespace:
    """A component that provides `key` and reads the keys in `deps`."""

    def apply(ctx: Context, config: object) -> Callable[[], None]:
        token = (name, next(probe.serial))
        for dep in deps:
            ctx[dep]
        ctx.provide(key, token)
        ctx.on(EVENT, lambda: None)

        def revert() -> None:
            if any(token in tokens for tokens in probe.installed.values()):
                probe.violations.append(
                    f"{name} reverted while a dependent was installed"
                )

        return revert

    return SimpleNamespace(name=name, inject=list(deps), apply=apply)


def dependent(
    probe: Probe, name: str, deps: tuple[str, ...], fail_on: str | None = None
) -> SimpleNamespace:
    """A component whose load has two iterations, each one reading `deps`.

    It raises `ValueError` when `fail_on` is a key of its config that is true.
    """

    async def apply(ctx: Context, config: dict[str, bool] | None) -> AsyncIterator[Any]:
        run = next(probe.serial)
        for _ in range(2):
            marker = object()
            tokens = tuple(ctx[dep] for dep in deps)
            probe.reads.setdefault(run, set()).add(tokens)
            probe.installed[marker] = tokens
            yield partial(probe.uninstall, marker)
            if fail_on is not None and config and config.get(fail_on):
                raise ValueError(f"{name} failed")
            await Deferred.resolved(None)

    return SimpleNamespace(name=name, inject=list(deps), apply=apply)


def plain(name: str, child: Any = None) -> SimpleNamespace:
    """A component with no dependencies. It can start one child component."""

    def apply(ctx: Context, config: object) -> None:
        ctx.on(EVENT, lambda: None)
        if child is not None:
            ctx.use(child)

    return SimpleNamespace(name=name, inject=[], apply=apply)


def build_pool(probe: Probe, prefix: str = "") -> list[Slot]:
    """Return the slots in listing order: dependents before the providers they need."""
    a, b = f"{prefix}a", f"{prefix}b"
    leaf = plain(f"{prefix}leaf")
    parent = dependent(probe, f"{prefix}parent", (a,))
    parent_apply = parent.apply

    async def parent_with_child(ctx: Context, config: object) -> AsyncIterator[Any]:
        ctx.use(leaf)
        async for effect in parent_apply(ctx, config):
            yield effect

    parent.apply = parent_with_child
    pool = [
        Slot(f"{prefix}c2", [dependent(probe, f"{prefix}c2", (a, b))]),
        Slot(f"{prefix}c1", [dependent(probe, f"{prefix}c1", (a,))]),
        Slot(f"{prefix}parent", [parent]),
        Slot(
            f"{prefix}b",
            [
                provider(probe, f"{prefix}b", b, (a,)),
                provider(probe, f"{prefix}b2", b, (a,)),
            ],
        ),
        Slot(
            f"{prefix}a",
            [provider(probe, f"{prefix}a", a), provider(probe, f"{prefix}a2", a)],
        ),
        Slot(f"{prefix}plain", [plain(f"{prefix}plain")]),
    ]
    for slot in pool:
        slot.provides = len(slot.variants) > 1
    return pool


def keys_of(prefix: str = "") -> list[str]:
    return [f"{prefix}a", f"{prefix}b"]


def random_ops(
    rng: random.Random, slots: list[Slot], count: int, first: tuple[Op, ...] = ()
) -> list[Op]:
    """Return calls that are valid for the model, whatever the schedule does.

    The calls in `first` come first. They must use slots that are not live.
    """
    ops: list[Op] = list(first)
    live = [False] * len(slots)
    replaced = [False] * len(slots)
    for _, index in first:
        live[index] = True
    for _ in range(count):
        index = rng.randrange(len(slots))
        if not live[index]:
            ops.append(("use", index))
            live[index] = True
        elif (
            len(slots[index].variants) > 1
            and not replaced[index]
            and rng.random() < 0.4
        ):
            ops.append(("replace", index))
            replaced[index] = True
        else:
            ops.append(("dispose", index))
            live[index] = False
    return ops


def apply_op(
    ctx: Context, pump: FramePumpScheduler, slots: list[Slot], op: Op, seed: int
) -> None:
    """Make one call.

    A realm has one provider for a key. After a provider is disposed, the schedule runs
    to quiescence, so that a later `use` of a provider of the same key does not conflict
    with a provider that is still unloading.
    """
    kind, index = op
    slot = slots[index]
    if kind == "use":
        slot.fiber = ctx.use(slot.current, slot.config)
        slot.live = True
    elif kind == "dispose":
        assert slot.fiber is not None
        slot.fiber.dispose()
        slot.live = False
        if slot.provides:
            settle(pump, seed)
    else:
        ctx.registry.replace(slot.variants[0], slot.variants[1])
        slot.replaced = True


def walk(fiber: Fiber) -> list[Fiber]:
    return [fiber, *(node for child in fiber.children for node in walk(child))]


def check_well_formed(root: Context, seed: int) -> None:
    providers: dict[str, int] = {}
    for fiber in walk(root.fiber):
        parent = fiber.parent.fiber
        assert parent is fiber or parent.state != FiberState.DISPOSED, (
            f"seed {seed}: {fiber.name} has a disposed parent"
        )
        for impl in fiber.provisions:
            providers[impl.name] = providers.get(impl.name, 0) + 1
    assert all(count == 1 for count in providers.values()), (
        f"seed {seed}: a key has more than one provider: {providers}"
    )


def settle(pump: FramePumpScheduler, seed: int) -> None:
    steps = pump.pump(QUIESCENCE_LIMIT)
    assert steps < QUIESCENCE_LIMIT, f"seed {seed}: no quiescence"


def run_schedule(
    root: Context,
    pump: FramePumpScheduler,
    slots: list[Slot],
    ops: list[Op],
    rng: random.Random,
    seed: int,
) -> None:
    """Make each call, and pump single steps between calls. Check after each step."""
    for op in ops:
        apply_op(root, pump, slots, op, seed)
        check_well_formed(root, seed)
        for _ in range(rng.randrange(4)):
            pump.pump(1)
            check_well_formed(root, seed)
    settle(pump, seed)


def structure(root: Context, keys: list[str]) -> tuple[Any, dict[str, str | None]]:
    """The observable state: the fiber tree, and the provider of each key."""

    def node(fiber: Fiber) -> Any:
        return (
            fiber.name,
            fiber.state.name,
            tuple(effect.label for effect in fiber.get_effects()),
            tuple(sorted(node(child) for child in fiber.children)),
        )

    providers = {
        key: (None if (token := root.get(key, strict=False)) is None else token[0])
        for key in keys
    }
    return node(root.fiber), providers


def new_root() -> tuple[Context, FramePumpScheduler, Probe]:
    pump = FramePumpScheduler()
    return Context(pump), pump, Probe()


def static_structure(slots: list[Slot], prefix: str = "") -> Any:
    """Load the live slots in listing order, with no schedule, and observe."""
    root, pump, _ = new_root()
    for slot in slots:
        if slot.live:
            root.use(slot.current, slot.config)
    settle(pump, -1)
    return structure(root, keys_of(prefix))


@pytest.mark.contract("EFF-REVERT")
@pytest.mark.parametrize("seed", SEEDS)
def test_disposing_every_fiber_restores_the_tables(seed: int) -> None:
    rng = random.Random(seed)
    root, pump, probe = new_root()
    before = hook_snapshot(root)
    slots = build_pool(probe)

    run_schedule(root, pump, slots, random_ops(rng, slots, OPS), rng, seed)
    for slot in slots:
        if slot.live:
            apply_op(root, pump, slots, ("dispose", slots.index(slot)), seed)
    settle(pump, seed)

    assert hook_snapshot(root) == before, f"seed {seed}"
    assert all(root.get(key, strict=False) is None for key in keys_of()), f"seed {seed}"
    assert root.fiber.children == (), f"seed {seed}"
    assert probe.installed == {}, f"seed {seed}"


@pytest.mark.contract("LIFE-ORDER")
@pytest.mark.parametrize("seed", SEEDS)
def test_a_provider_never_reverts_under_an_installed_dependent(seed: int) -> None:
    rng = random.Random(seed)
    root, pump, probe = new_root()
    slots = build_pool(probe)

    run_schedule(root, pump, slots, random_ops(rng, slots, OPS), rng, seed)

    assert probe.violations == [], f"seed {seed}"


@pytest.mark.contract("LIFE-PROGRESS")
@pytest.mark.parametrize("seed", SEEDS)
def test_every_schedule_reaches_quiescence_with_each_fiber_in_its_right_state(
    seed: int,
) -> None:
    rng = random.Random(seed)
    root, pump, probe = new_root()
    slots = build_pool(probe)

    run_schedule(root, pump, slots, random_ops(rng, slots, OPS), rng, seed)

    for slot in slots:
        if not slot.live:
            continue
        assert slot.fiber is not None
        ready = all(
            root.get(dep, strict=False) is not None for dep in slot.fiber.inject
        )
        expected = FiberState.ACTIVE if ready else FiberState.PENDING
        assert slot.fiber.state == expected, f"seed {seed}: {slot.name}"


@pytest.mark.contract("LIFE-CONFLUENT")
@pytest.mark.parametrize("seed", SEEDS)
def test_a_schedule_ends_where_a_static_load_of_the_same_components_ends(
    seed: int,
) -> None:
    rng = random.Random(seed)
    root, pump, probe = new_root()
    slots = build_pool(probe)

    run_schedule(root, pump, slots, random_ops(rng, slots, OPS), rng, seed)

    assert structure(root, keys_of()) == static_structure(slots), f"seed {seed}"


@pytest.mark.contract("EFF-ISOLATE")
@pytest.mark.parametrize("seed", SEEDS)
def test_components_at_different_keys_interleave_as_they_run_one_after_the_other(
    seed: int,
) -> None:
    rng = random.Random(seed)
    probe = Probe()
    group_x, group_y = build_pool(probe, "x"), build_pool(probe, "y")
    ops_x = random_ops(rng, group_x, OPS)
    ops_y = random_ops(rng, group_y, OPS)

    def run(order: list[tuple[list[Slot], Op]], pumps: random.Random | None) -> Any:
        root, pump, _ = new_root()
        for slots, op in order:
            apply_op(root, pump, slots, op, seed)
            if pumps is not None:
                pump.pump(pumps.randrange(4))
        settle(pump, seed)
        return structure(root, keys_of("x") + keys_of("y"))

    sequential = [(group_x, op) for op in ops_x] + [(group_y, op) for op in ops_y]
    merged: list[tuple[list[Slot], Op]] = []
    pending = {id(group_x): list(ops_x), id(group_y): list(ops_y)}
    groups = {id(group_x): group_x, id(group_y): group_y}
    while any(pending.values()):
        gid = rng.choice([g for g, queue in pending.items() if queue])
        merged.append((groups[gid], pending[gid].pop(0)))

    interleaved_result = run(merged, random.Random(seed))
    for slots in (group_x, group_y):
        for slot in slots:
            slot.live = slot.replaced = False
            slot.fiber = None
    sequential_result = run(sequential, None)

    assert interleaved_result == sequential_result, f"seed {seed}"


@pytest.mark.contract("REG-WELLFORMED")
@pytest.mark.parametrize("seed", SEEDS)
def test_the_registry_is_well_formed_after_every_step(seed: int) -> None:
    rng = random.Random(seed)
    root, pump, probe = new_root()
    slots = build_pool(probe)

    # `run_schedule` checks the registry after each call and each single step
    run_schedule(root, pump, slots, random_ops(rng, slots, OPS), rng, seed)
    check_well_formed(root, seed)


@pytest.mark.contract("LIFE-COHERENT")
@pytest.mark.parametrize("seed", SEEDS)
def test_every_iteration_of_a_load_reads_one_resolution(seed: int) -> None:
    rng = random.Random(seed)
    root, pump, probe = new_root()
    slots = build_pool(probe)

    # a dependent and its provider load first, so that every seed has loads to check
    first = (("use", 1), ("use", 4))
    ops = random_ops(rng, slots, OPS, first)
    for op in first:
        apply_op(root, pump, slots, op, seed)
    settle(pump, seed)
    run_schedule(root, pump, slots, ops[len(first) :], rng, seed)

    assert probe.reads
    assert all(len(resolutions) == 1 for resolutions in probe.reads.values()), (
        f"seed {seed}"
    )


@pytest.mark.contract("LIFE-FAILURE")
@pytest.mark.parametrize("seed", SEEDS)
def test_a_failed_fiber_leaves_its_siblings_unchanged_and_update_recovers_it(
    seed: int,
) -> None:
    rng = random.Random(seed)
    probe = Probe()
    slots = build_pool(probe)
    failing = Slot("failing", [dependent(probe, "failing", ("a",), fail_on="fail")])
    failing.config = {"fail": True}
    ops = random_ops(rng, slots + [failing], OPS)

    def run(with_failing: bool) -> tuple[Context, FramePumpScheduler, list[Slot]]:
        root, pump, _ = new_root()
        pool = [*build_pool(probe), failing] if with_failing else build_pool(probe)
        failing.live = failing.replaced = False
        failing.fiber = None
        run_schedule(
            root,
            pump,
            pool,
            [op for op in ops if with_failing or op[1] < len(slots)],
            random.Random(seed),
            seed,
        )
        return root, pump, pool

    root, pump, _ = run(True)
    failed = failing.live and root.get("a", strict=False) is not None
    if failed:
        assert failing.fiber is not None
        assert failing.fiber.state == FiberState.FAILED, f"seed {seed}"
        assert isinstance(failing.fiber.error, ValueError), f"seed {seed}"
        assert failing.fiber.get_effects() == [], f"seed {seed}"
    with_failure = structure(root, keys_of())
    # the failed fiber is outside the comparison: remove it, then compare the rest
    if failing.live:
        assert failing.fiber is not None
        failing.fiber.dispose()
        settle(pump, seed)
    after_removal = structure(root, keys_of())
    reference_root, _, _ = run(False)

    assert after_removal == structure(reference_root, keys_of()), f"seed {seed}"
    assert with_failure[1] == after_removal[1], f"seed {seed}"
    assert probe.violations == [], f"seed {seed}"
