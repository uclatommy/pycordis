"""Rebuilds: an entry that changes its name or its `inject` loads its component once."""

from collections.abc import Callable
from typing import Any

import pytest
from loader_fakes import Mock, RecordingLoader

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import FiberState

PING = "rebuild-ping"


def provide_settings(ctx: Context, config: Any) -> None:
    ctx.provide("settings", {})


def active(loader: RecordingLoader, component: Mock) -> int:
    """Return the number of fibers of `component` that are active."""
    runtime = loader.ctx.registry.get(component)
    if runtime is None:
        return 0
    return sum(fiber.state == FiberState.ACTIVE for fiber in runtime.fibers)


class Parts:
    """The components `settings`, `a`, `b`, and `c`, and the disposer of `a`."""

    def __init__(self, loader: RecordingLoader) -> None:
        self.loader = loader
        self.dispose_a: Callable[[], Any] = lambda: None
        loader.mock("settings", provide_settings)
        self.a = loader.mock("a", lambda ctx, config: lambda: self.dispose_a())
        self.b = loader.mock("b")
        self.c = loader.mock("c")

    def replay(self, name: str, inject: list[str] | None) -> Deferred[None]:
        """Give the group the data of a file reload: `x` has a new name."""
        entry: dict[str, Any] = {"id": "x", "name": name}
        if inject is not None:
            entry["inject"] = inject
        return self.loader.read([{"id": "settings", "name": "settings"}, entry])

    def hold_disposal_of_a(self) -> tuple[Deferred[None], Deferred[None]]:
        """Make the disposal of `a` wait. Return the entry signal and the release."""
        entered, held = Deferred[None](), Deferred[None]()

        def dispose() -> Deferred[None]:
            entered.resolve(None)
            return held

        self.dispose_a = dispose
        return entered, held


@pytest.fixture
def parts(loader: RecordingLoader) -> Parts:
    return Parts(loader)


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: entry rebuild::rebuilds an active entry when its name changes (inject=undefined)"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: entry rebuild::rebuilds an active entry when its name changes (inject=[ 'settings' ])"  # noqa: E501
)
@pytest.mark.parametrize("inject", [None, ["settings"]])
def test_a_new_name_loads_the_new_component_and_keeps_the_options(
    loader: RecordingLoader, parts: Parts, inject: list[str] | None
) -> None:
    parts.replay("a", inject).result()
    previous = loader.fiber("x")

    parts.replay("b", inject).result()

    assert previous.uid is None
    assert loader.fiber("x").state == FiberState.ACTIVE
    assert (len(parts.a.calls), len(parts.b.calls)) == (1, 1)
    assert "disabled" not in loader.resolve("x").options
    assert all(not (change.options or {}).get("disabled") for change in loader.changes)


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: entry rebuild::uses the new name when it changes during the import"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::uses the latest name during a pending import"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::uses the latest name during a pending failed import"  # noqa: E501
)
@pytest.mark.parametrize("fails", [False, True])
def test_the_latest_name_loads_once_when_the_name_changes_during_an_import(
    loader: RecordingLoader, parts: Parts, fails: bool
) -> None:
    gate = Deferred[None]()
    loader.gate = lambda name: gate if name == "b" else None
    loader.read([{"id": "x", "name": "a"}]).result()

    first = loader.read([{"id": "x", "name": "b"}])
    second = loader.read([{"id": "x", "name": "c"}])
    assert loader.busy()
    if fails:
        del loader.modules["b"]
    gate.resolve(None)

    first.result()
    second.result()
    assert loader.resolve("x").options["name"] == "c"
    assert (len(parts.b.calls), len(parts.c.calls)) == (0, 1)
    assert loader.fiber("x").runtime.callback is parts.c  # type: ignore[union-attr]
    assert "disabled" not in loader.resolve("x").options


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: entry rebuild::joins a second rename while the first disposal is pending (inject=undefined)"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: entry rebuild::joins a second rename while the first disposal is pending (inject=[ 'settings' ])"  # noqa: E501
)
@pytest.mark.parametrize("inject", [None, ["settings"]])
def test_a_second_rename_joins_the_rebuild_that_waits_for_a_disposal(
    loader: RecordingLoader, parts: Parts, inject: list[str] | None
) -> None:
    entered, held = parts.hold_disposal_of_a()
    parts.replay("a", inject).result()

    first = parts.replay("b", inject)
    assert entered.done()
    second = parts.replay("c", inject)
    held.resolve(None)

    first.result()
    second.result()
    assert (len(parts.b.calls), len(parts.c.calls)) == (0, 1)
    assert loader.fiber("x").state == FiberState.ACTIVE


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: entry rebuild::applies nothing when the entry is removed during the disposal"  # noqa: E501
)
def test_an_entry_that_is_removed_during_a_rebuild_loads_nothing(
    loader: RecordingLoader, parts: Parts
) -> None:
    entered, held = parts.hold_disposal_of_a()
    parts.replay("a", ["settings"]).result()
    entry = loader.resolve("x")

    renaming = parts.replay("b", ["settings"])
    assert entered.done()
    loader.remove("x")
    held.resolve(None)

    renaming.result()
    assert parts.b.calls == []
    assert entry.outer_fiber is None
    assert entry.live_fiber() is None


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: entry rebuild::rebuilds when the component renames its own entry"  # noqa: E501
)
def test_a_component_can_rename_its_own_entry(
    loader: RecordingLoader, parts: Parts
) -> None:
    held = Deferred[None]()

    def rename(ctx: Context, config: Any) -> Callable[[], Deferred[None]]:
        loader.update("x", {"name": "b"})
        return lambda: held

    renaming = loader.mock("renaming", rename)
    loader.create({"id": "x", "name": "renaming"})

    # the rebuild is a task until the old instance is disposed
    assert loader.busy()
    assert parts.b.calls == []
    held.resolve(None)
    loader.wait().result()
    assert len(renaming.calls) == 1
    assert len(parts.b.calls) == 1
    assert loader.fiber("x").state == FiberState.ACTIVE


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::starts a pending entry when inject is cleared"  # noqa: E501
)
def test_clearing_inject_starts_an_entry_that_waits_for_a_service(
    loader: RecordingLoader,
) -> None:
    plug = loader.mock("plug")
    identifier = loader.create({"name": "plug", "inject": ["never"]}).result()
    entry = loader.resolve(identifier)
    old = entry.outer_fiber
    assert old is not None and old.state == FiberState.PENDING
    assert entry.inner_fiber is None
    assert plug.calls == []

    loader.changes.clear()
    loader.update(identifier, {"inject": None}).result()

    assert loader.fiber(identifier).state == FiberState.ACTIVE
    assert old.state == FiberState.DISPOSED
    assert len(plug.calls) == 1
    assert "inject" not in entry.options
    assert "disabled" not in entry.options
    assert not any((change.options or {}).get("disabled") for change in loader.changes)


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::reapplies when inject gains a service that is already provided"  # noqa: E501
)
def test_adding_inject_loads_the_component_again_with_the_service(
    root: Context, loader: RecordingLoader
) -> None:
    root.provide("foo", {"n": 1})
    reads: list[Any] = []
    plug = loader.mock("plug", lambda ctx, config: reads.append(ctx.get("foo")))
    identifier = loader.create({"name": "plug"}).result()
    old = loader.fiber(identifier)
    assert len(plug.calls) == 1

    loader.update(identifier, {"inject": ["foo"]}).result()

    assert len(plug.calls) == 2
    assert reads[1] == {"n": 1}
    assert old.state == FiberState.DISPOSED
    assert loader.fiber(identifier) is not old
    assert "disabled" not in loader.resolve(identifier).options


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::keeps the new outer fiber pending and drops the old listener when inject is missing"  # noqa: E501
)
def test_a_rebuild_to_a_missing_service_removes_the_listeners_of_the_old_component(
    root: Context, loader: RecordingLoader
) -> None:
    hits: list[None] = []
    loader.mock("plug", lambda ctx, config: ctx.on(PING, lambda: hits.append(None)))
    identifier = loader.create({"name": "plug"}).result()
    old = loader.fiber(identifier)
    root.emit(PING)
    assert len(hits) == 1

    loader.update(identifier, {"inject": ["foo"]}).result()

    entry = loader.resolve(identifier)
    assert old.state == FiberState.DISPOSED
    assert entry.outer_fiber is not None
    assert entry.outer_fiber.state == FiberState.PENDING
    assert entry.live_fiber() is None
    root.emit(PING)
    assert len(hits) == 1
    assert "disabled" not in entry.options


def gated_plug(loader: RecordingLoader, gate: Deferred[None]) -> tuple[Mock, list[int]]:
    """Return a component whose disposer waits for `gate`, and its list of applies."""
    applies: list[int] = []

    def apply(ctx: Context, config: Any) -> Callable[[], Deferred[None]]:
        applies.append(1)
        return lambda: gate

    return loader.mock("plug", apply), applies


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::joins a second inject update while the first rebuild is disposing"  # noqa: E501
)
def test_a_second_inject_update_joins_the_rebuild_that_is_disposing(
    root: Context, loader: RecordingLoader
) -> None:
    root.provide("foo", {"n": 1})
    root.provide("bar", {"n": 2})
    gate = Deferred[None]()
    plug, applies = gated_plug(loader, gate)
    identifier = loader.create({"name": "plug"}).result()
    assert len(applies) == 1

    first = loader.update(identifier, {"inject": ["foo"]})
    second = loader.update(identifier, {"inject": ["bar"]})
    assert loader.busy()
    assert len(applies) == 1
    gate.resolve(None)

    first.result()
    second.result()
    assert len(applies) == 2
    assert loader.resolve(identifier).options["inject"] == ["bar"]
    assert active(loader, plug) == 1
    assert loader.fiber(identifier).state == FiberState.ACTIVE
    assert "disabled" not in loader.resolve(identifier).options


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::does not apply a fiber removed while rebuild is disposing"  # noqa: E501
)
def test_a_fiber_that_is_removed_while_its_rebuild_disposes_never_loads_again(
    root: Context, loader: RecordingLoader
) -> None:
    root.provide("foo", {"n": 1})
    gate = Deferred[None]()
    plug, applies = gated_plug(loader, gate)
    identifier = loader.create({"name": "plug"}).result()

    pending = loader.update(identifier, {"inject": ["foo"]})
    loader.remove(identifier)
    gate.resolve(None)

    pending.result()
    assert len(applies) == 1
    assert [entry.id for entry in loader.entries()] == []
    assert loader.ctx.registry.get(plug) is None


@pytest.mark.upstream(
    "loader/rebuild.spec.ts::Loader: active entry rebuild (#218)::keeps a rebuild started during apply until its disposer settles"  # noqa: E501
)
def test_a_rebuild_that_a_component_starts_during_its_apply_keeps_the_loader_busy(
    root: Context, loader: RecordingLoader
) -> None:
    for name in ("foo", "bar", "qux"):
        root.provide(name, {})
    gate = Deferred[None]()
    applies: list[int] = []
    identifier = ""

    def apply(ctx: Context, config: Any) -> Callable[[], Deferred[None] | None]:
        applies.append(1)
        number = len(applies)
        if number == 2:
            loader.update(identifier, {"inject": ["bar"]})
        return lambda: gate if number == 2 else None

    plug = loader.mock("plug", apply)
    identifier = loader.create({"name": "plug"}).result()
    loader.update(identifier, {"inject": ["foo"]})

    assert loader.busy()
    waiting = loader.wait()
    assert not waiting.done()
    assert len(applies) == 2

    third = loader.update(identifier, {"inject": ["qux"]})
    assert len(applies) == 2
    gate.resolve(None)

    third.result()
    waiting.result()
    assert len(applies) == 3
    assert loader.resolve(identifier).options["inject"] == ["qux"]
    assert active(loader, plug) == 1
    assert "disabled" not in loader.resolve(identifier).options
