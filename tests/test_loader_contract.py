"""Contract properties of the loader under seeded random schedules.

Each test applies a random list of host calls (`create`, `update`, a move, `remove`,
and the self-dispose of a component) to a loader on a `FramePumpScheduler`, and pumps a
random number of steps between the calls. The seed is part of every assertion message.
"""

import random
from dataclasses import dataclass, field
from typing import Any

import pytest
from loader_fakes import GROUP, RecordingLoader

from pycordis.context import Context
from pycordis.scheduler import FramePumpScheduler

SEEDS = range(30)
OPS = 30
QUIESCENCE_LIMIT = 10_000

GROUPS = ("g1", "g2")
# leaf id -> the component names that the entry can use
LEAVES = {
    "a": ("foo", "bar"),
    "b": ("foo", "bar"),
    "c": ("foo", "bar"),
    "d": ("dep",),
    "p": ("svc",),
}

type Op = tuple[Any, ...]


@dataclass
class Model:
    """The tree that the host asked for: the options and the parent of each entry."""

    options: dict[str, dict[str, Any]] = field(default_factory=dict)
    parent: dict[str, str | None] = field(default_factory=dict)

    def children(self, parent: str | None) -> list[str]:
        return [name for name, owner in self.parent.items() if owner == parent]

    def runs(self, name: str) -> bool:
        """Return whether the host data lets the component of `name` run.

        Only an entry with no service to wait for has a certain answer: it runs unless
        it or a group above it is disabled.
        """
        chain = [name]
        while (owner := self.parent[chain[-1]]) is not None:
            chain.append(owner)
        return not any(self.options[item].get("disabled") for item in chain)

    def data(self, parent: str | None = None) -> list[dict[str, Any]]:
        """Return the options of the members of `parent`, with groups expanded."""
        result = []
        for name in self.children(parent):
            options = dict(self.options[name])
            if options.get("group"):
                options["config"] = self.data(name)
            result.append(options)
        return result


def generate_ops(rng: random.Random) -> list[Op]:
    """Return random host calls that are valid for the tree that they build."""
    leaves: dict[str, str | None] = {}
    groups = list(GROUPS)
    ops: list[Op] = []
    while len(ops) < OPS:
        absent = [name for name in LEAVES if name not in leaves]
        kind = rng.choice(
            ["create", "create", "config", "rename", "inject", "disable", "move"]
            + ["remove", "selfdispose"]
        )
        parents = [None, *groups]
        if kind == "create" and absent:
            name = rng.choice(absent)
            leaves[name] = rng.choice(parents)
            ops.append(("create", name, leaves[name], rng.choice(LEAVES[name])))
        elif kind == "disable" and (leaves or groups):
            ops.append(("disable", rng.choice([*leaves, *groups]), rng.random() < 0.6))
        elif kind == "remove" and (leaves or groups):
            name = rng.choice([*leaves, *groups])
            if name in groups:
                groups.remove(name)
                leaves = {k: v for k, v in leaves.items() if v != name}
            else:
                del leaves[name]
            ops.append(("remove", name))
        elif leaves and kind == "config":
            ops.append(("config", rng.choice(list(leaves)), rng.randrange(3)))
        elif leaves and kind == "rename":
            name = rng.choice(
                [name for name in leaves if len(LEAVES[name]) > 1] or [""]
            )
            if name:
                ops.append(("rename", name))
        elif leaves and kind == "inject":
            ops.append(("inject", rng.choice(list(leaves)), rng.random() < 0.5))
        elif leaves and kind == "move":
            name = rng.choice(list(leaves))
            leaves[name] = rng.choice(parents)
            ops.append(("move", name, leaves[name]))
        elif leaves and kind == "selfdispose":
            ops.append(("selfdispose", rng.choice(list(leaves))))
    return ops


class Run:
    """A loader on a pumped scheduler, the model of the host calls, and the pump."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.pump = FramePumpScheduler()
        self.root = Context(self.pump)
        self.root.use(RecordingLoader)
        self.settle()
        self.loader: RecordingLoader = self.root.get("loader")  # type: ignore[assignment]
        self.loader.mock("foo")
        self.loader.mock("bar")
        self.loader.mock("dep", inject=["svc"])
        self.loader.mock("svc", lambda ctx, _: ctx.provide("svc", object()))
        self.model = Model()

    def settle(self) -> None:
        steps = self.pump.pump(QUIESCENCE_LIMIT)
        assert steps < QUIESCENCE_LIMIT, f"seed {self.seed}: no quiescence"

    def create_groups(self) -> None:
        """Create the groups and let them start: a group accepts members only then."""
        for name in GROUPS:
            options = {"id": name, "name": GROUP, "group": True, "config": []}
            self.loader.create(options)
            self.model.options[name] = {"id": name, "name": GROUP, "group": True}
            self.model.parent[name] = None
        self.settle()

    def apply(self, op: Op) -> None:
        loader, model = self.loader, self.model
        kind, name, *rest = op
        if kind == "create":
            parent, component = rest
            options = {"id": name, "name": component, "config": {"n": 0}}
            loader.create(options, parent)
            model.options[name] = options
            model.parent[name] = parent
        elif kind == "remove":
            loader.remove(name)
            doomed = [name, *model.children(name)]
            for item in doomed:
                del model.options[item], model.parent[item]
        elif kind == "selfdispose":
            self.settle()
            fiber = loader.resolve(name).live_fiber() if name in model.options else None
            if fiber is not None:
                fiber.dispose()
                model.options[name]["disabled"] = True
        else:
            self.change(kind, name, rest)

    def change(self, kind: str, name: str, rest: list[Any]) -> None:
        loader, options = self.loader, self.model.options[name]
        changes: dict[str, Any]
        if kind == "disable":
            changes = {"disabled": True if rest[0] else None}
        elif kind == "config":
            changes = {"config": {"n": rest[0]}}
        elif kind == "rename":
            changes = {"name": "bar" if options["name"] == "foo" else "foo"}
        elif kind == "inject":
            changes = {"inject": ["svc"] if rest[0] else None}
        else:
            changes = {}
        if kind == "move":
            loader.update(name, changes, rest[0])
            self.model.parent[name] = rest[0]
        else:
            loader.update(name, changes)
        for key, value in changes.items():
            if value is None:
                options.pop(key, None)
            else:
                options[key] = value

    def observe(self) -> dict[str, Any]:
        """Return the options tree and the fiber states of every entry."""
        states = {}
        for entry in self.loader.entries():
            outer, inner = entry.outer_fiber, entry.live_fiber()
            states[entry.options["id"]] = (
                None if outer is None else outer.state,
                None if inner is None else inner.state,
            )
        return {"tree": normalize(self.loader.root.data), "states": states}


def normalize(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return `data` with the members of every group in the order of their ids."""
    result = []
    for options in sorted(data, key=lambda item: item["id"]):
        options = dict(options)
        if options.get("group"):
            options["config"] = normalize(options["config"])
        result.append(options)
    return result


def run_schedule(
    seed: int, ops: list[Op], pumps: random.Random | None
) -> tuple[Run, dict[str, Any]]:
    """Apply `ops`, pump a random number of steps after each one, and observe."""
    run = Run(seed)
    run.create_groups()
    for op in ops:
        try:
            run.apply(op)
        except Exception as error:
            error.add_note(f"seed {seed}, op {op}")
            raise
        if pumps is not None:
            run.pump.pump(pumps.randrange(4))
    run.settle()
    return run, run.observe()


def static_load(seed: int, model: Model) -> dict[str, Any]:
    """Load the data of `model` into a new loader at once, and observe."""
    run = Run(seed)
    run.loader.read(model.data())
    run.settle()
    return run.observe()


@pytest.mark.contract("LIFE-CONFLUENT")
@pytest.mark.parametrize("seed", SEEDS)
def test_a_tree_change_schedule_ends_where_a_static_load_of_its_data_ends(
    seed: int,
) -> None:
    ops = generate_ops(random.Random(seed))

    run, observed = run_schedule(seed, ops, random.Random(seed + 1000))

    assert observed == static_load(seed, run.model), f"seed {seed}"
    # both loads use the same code, so the model is the check of what runs
    for name, options in run.model.options.items():
        if options["name"] in ("foo", "bar") and "inject" not in options:
            running = observed["states"][name][1] is not None
            assert running == run.model.runs(name), f"seed {seed}: {name}"


@pytest.mark.contract("LIFE-CONFLUENT")
@pytest.mark.parametrize("seed", SEEDS)
def test_the_end_state_does_not_depend_on_the_steps_between_the_calls(
    seed: int,
) -> None:
    ops = generate_ops(random.Random(seed))

    _, without_pumps = run_schedule(seed, ops, None)
    _, with_pumps = run_schedule(seed, ops, random.Random(seed + 1000))
    _, other_pumps = run_schedule(seed, ops, random.Random(seed + 2000))

    assert without_pumps == with_pumps == other_pumps, f"seed {seed}"


@pytest.mark.contract("LOAD-OPTIONS")
@pytest.mark.parametrize("seed", SEEDS)
def test_the_options_of_an_entry_differ_from_the_host_data_only_after_a_self_dispose(
    seed: int,
) -> None:
    ops = generate_ops(random.Random(seed))

    run, observed = run_schedule(seed, ops, random.Random(seed + 1000))

    # the model has `disabled` only for a host call or a self-dispose that it saw
    assert observed["tree"] == normalize(run.model.data()), f"seed {seed}"
