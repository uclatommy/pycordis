"""Entry `inject`: the outer fiber, the verdict, and entries that drop out of a load."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from loader_fakes import GROUP, Mock, RecordingLoader

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import FiberState

SETTINGS = {"id": "settings", "name": "settings"}


def provide_settings(ctx: Context, config: Any) -> None:
    ctx.provide("settings", dict(config or {}))


def worker_entry(**options: Any) -> dict[str, Any]:
    return {"id": "worker", "name": "worker", **options}


@pytest.fixture
def worker(loader: RecordingLoader) -> Mock:
    loader.mock("settings", provide_settings)
    return loader.mock("worker")


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::creates no outer fiber for an entry without inject"  # noqa: E501
)
def test_an_entry_without_inject_has_no_outer_fiber(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([worker_entry(disabled=False)]).result()

    entry = loader.resolve("worker")
    assert entry.outer_fiber is None
    assert entry.inject == {}
    assert loader.fiber("worker").state == FiberState.ACTIVE
    assert len(worker.calls) == 1


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::nests the inner fiber under the outer fiber"  # noqa: E501
)
def test_the_component_runs_in_a_child_of_the_fiber_that_injects_the_services(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([SETTINGS, worker_entry(inject=["settings"])]).result()

    entry = loader.resolve("worker")
    outer, inner = entry.outer_fiber, entry.inner_fiber
    assert outer is not None and inner is not None
    assert inner.parent.fiber is outer
    assert list(outer.inject) == ["settings"]
    assert list(inner.inject) == []
    assert loader.entry_of(outer) is None
    assert loader.entry_of(inner) is entry


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::waits for the entry inject while it is missing"  # noqa: E501
)
def test_an_entry_waits_for_a_service_that_its_component_does_not_declare(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([worker_entry(inject=["settings"])]).result()

    entry = loader.resolve("worker")
    assert entry.outer_fiber is not None
    assert entry.outer_fiber.state == FiberState.PENDING
    assert entry.disabled
    assert worker.calls == []

    loader.create(SETTINGS).result()

    assert not entry.disabled
    assert len(worker.calls) == 1


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::keeps an inject value on the outer fiber when the component declares the service itself"  # noqa: E501
)
def test_the_inject_of_an_entry_does_not_replace_the_inject_of_its_component(
    loader: RecordingLoader, worker: Mock
) -> None:
    own = loader.mock("own", inject={"settings": {"own": 1}})

    loader.read(
        [SETTINGS, {"id": "own", "name": "own", "inject": {"settings": {"ns": "ns"}}}]
    ).result()

    entry = loader.resolve("own")
    assert len(own.calls) == 1
    assert entry.outer_fiber is not None and entry.inner_fiber is not None
    assert entry.outer_fiber.inject == {"settings": {"ns": "ns"}}
    assert entry.inner_fiber.inject == {"settings": {"own": 1}}


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::keeps the options when a dependency is lost and recovered"  # noqa: E501
)
def test_losing_a_service_unloads_the_component_and_keeps_the_options(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([SETTINGS, worker_entry(inject=["settings"], disabled=False)]).result()
    entry = loader.resolve("worker")
    outer, inner = entry.outer_fiber, entry.inner_fiber
    assert inner is not None

    loader.remove("settings")
    loader.wait().result()

    assert inner.uid is None
    assert entry.outer_fiber is outer
    assert entry.options["disabled"] is False
    assert [change.id for change in loader.changes] == ["settings"]

    loader.create(SETTINGS).result()

    assert loader.fiber("worker").state == FiberState.ACTIVE
    assert len(worker.calls) == 2


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::rebuilds the entry when inject changes"  # noqa: E501
)
def test_a_new_inject_loads_the_component_again_with_a_new_outer_fiber(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.mock("env", lambda ctx, config: ctx.provide("env", {}))
    loader.read(
        [SETTINGS, {"id": "env", "name": "env"}, worker_entry(inject=["settings"])]
    ).result()
    entry = loader.resolve("worker")
    outer = entry.outer_fiber
    assert outer is not None

    loader.update("worker", {"inject": ["settings", "env"]}).result()

    assert outer.uid is None
    assert entry.outer_fiber is not None
    assert list(entry.outer_fiber.inject) == ["settings", "env"]

    loader.update("worker", {"inject": None}).result()

    assert entry.outer_fiber is None
    assert entry.inner_fiber is not None
    assert entry.inner_fiber.parent.fiber is loader.ctx.fiber
    assert len(worker.calls) == 3


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::rebuilds without a write-back when inject is added"  # noqa: E501
)
def test_adding_inject_does_not_report_a_change_of_disabled(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([SETTINGS, worker_entry()]).result()
    loader.changes.clear()

    loader.update("worker", {"inject": ["settings"]}).result()

    entry = loader.resolve("worker")
    assert "disabled" not in entry.options
    assert all(not (change.options or {}).get("disabled") for change in loader.changes)
    assert loader.fiber("worker").state == FiberState.ACTIVE
    assert len(worker.calls) == 2


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::disposes the outer fiber when the component disposes itself"  # noqa: E501
)
def test_a_component_that_disposes_itself_takes_the_outer_fiber_with_it(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([SETTINGS, worker_entry(inject=["settings"])]).result()
    entry = loader.resolve("worker")
    outer = entry.outer_fiber
    assert outer is not None

    loader.fiber("worker").dispose()
    loader.wait().result()

    assert entry.options["disabled"] is True
    assert outer.uid is None
    assert entry.outer_fiber is None


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::disposes both fibers when the entry is removed"  # noqa: E501
)
def test_removing_an_entry_disposes_its_outer_fiber_and_its_component(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([SETTINGS, worker_entry(inject=["settings"])]).result()
    entry = loader.resolve("worker")
    outer, inner = entry.outer_fiber, entry.inner_fiber
    assert outer is not None and inner is not None

    loader.remove("worker")
    loader.wait().result()

    assert outer.uid is None
    assert inner.uid is None


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::keeps a group structural and passes its verdict to the children"  # noqa: E501
)
def test_a_disabled_group_with_inject_runs_and_its_children_wait_for_it(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read(
        [
            SETTINGS,
            {
                "id": "group",
                "name": GROUP,
                "group": True,
                "inject": ["settings"],
                "disabled": True,
                "config": [worker_entry()],
            },
        ]
    ).result()

    group = loader.resolve("group")
    assert group.inner_fiber is not None
    assert group.inner_fiber.state == FiberState.ACTIVE
    assert loader.resolve("worker").live_fiber() is None

    loader.update("group", {"disabled": False}).result()

    assert loader.fiber("worker").state == FiberState.ACTIVE
    assert group.options["disabled"] is False


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::invalidates the verdict before an asynchronous dependency unload"  # noqa: E501
)
def test_an_entry_is_disabled_while_its_component_unloads_after_a_lost_service(
    loader: RecordingLoader, worker: Mock
) -> None:
    release = Deferred[None]()
    seen: list[bool] = []

    def hold(ctx: Context, config: Any) -> Callable[[], Deferred[None]]:
        def unload() -> Deferred[None]:
            seen.append(loader.resolve("held").disabled)
            return release

        return unload

    loader.mock("held", hold)
    loader.read(
        [SETTINGS, {"id": "held", "name": "held", "inject": ["settings"]}]
    ).result()
    entry = loader.resolve("held")
    outer = entry.outer_fiber

    loader.remove("settings")

    assert seen == [True]
    assert entry.disabled
    assert entry.outer_fiber is outer
    assert "disabled" not in entry.options
    release.resolve(None)
    loader.wait().result()

    # an update while the outer fiber waits keeps it
    loader.update("held", {"config": {"value": 1}}).result()
    assert entry.outer_fiber is outer

    loader.create(SETTINGS).result()

    assert not entry.disabled
    assert loader.fiber("held").state == FiberState.ACTIVE


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::drops an activation whose dependency is lost during the import"  # noqa: E501
)
def test_a_service_that_is_lost_during_the_import_of_the_outer_fiber_stops_the_start(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([SETTINGS]).result()
    gate = Deferred[None]()
    imports: list[str] = []

    def hold_second_import(name: str) -> Deferred[None] | None:
        if name == "worker":
            imports.append(name)
        return gate if name == "worker" and len(imports) == 2 else None

    loader.gate = hold_second_import

    loader.create(worker_entry(inject=["settings"]))
    assert len(imports) == 2
    loader.remove("settings")
    gate.resolve(None)
    loader.wait().result()

    assert worker.calls == []
    outer = loader.resolve("worker").outer_fiber
    assert outer is not None and outer.state == FiberState.PENDING


@pytest.mark.upstream(
    "loader/evaluate.spec.ts::Loader: entry evaluation context::drops entries removed or disabled during import"  # noqa: E501
)
@pytest.mark.parametrize("action", ["remove", "disable"])
@pytest.mark.parametrize("inject", [None, ["settings"]])
def test_an_entry_that_is_removed_or_disabled_during_its_import_loads_nothing(
    loader: RecordingLoader, worker: Mock, inject: list[str] | None, action: str
) -> None:
    loader.read([SETTINGS]).result()
    release = Deferred[None]()
    loader.gate = lambda name: release if name == "worker" else None
    options = worker_entry(**({} if inject is None else {"inject": inject}))

    creating = loader.create(options)
    entry = loader.resolve("worker")
    if action == "remove":
        loader.remove("worker")
    else:
        loader.update("worker", {"disabled": True})
    release.resolve(None)
    creating.result()
    loader.wait().result()

    assert entry.outer_fiber is None
    assert entry.inner_fiber is None
    assert worker.calls == []

    # a dropped load does not block a later create or enable
    if action == "remove":
        loader.create(options).result()
    else:
        loader.update("worker", {"disabled": False}).result()
    loader.wait().result()

    assert loader.fiber("worker").state == FiberState.ACTIVE
    assert len(worker.calls) == 1


def test_an_update_starts_a_component_whose_import_failed_behind_the_outer_fiber(
    loader: RecordingLoader, worker: Mock
) -> None:
    loader.read([SETTINGS]).result()
    imports: list[str] = []

    def fail_second_import(name: str) -> None:
        if name == "worker":
            imports.append(name)
            if len(imports) == 2:
                del loader.modules["worker"]

    loader.gate = fail_second_import
    loader.read([SETTINGS, worker_entry(inject=["settings"])]).result()
    entry = loader.resolve("worker")
    assert entry.outer_fiber is not None
    assert entry.outer_fiber.state == FiberState.ACTIVE
    assert entry.inner_fiber is None

    loader.modules["worker"] = worker
    loader.update("worker", {"config": {"a": 1}}).result()

    assert worker.calls == [{"a": 1}]
    assert loader.fiber("worker").state == FiberState.ACTIVE


def test_an_intercept_layer_of_the_reader_can_set_await(
    root: Context, loader: RecordingLoader
) -> None:
    gate = Deferred[None]()

    async def slow(ctx: Context, config: Any) -> None:
        await gate

    loader.mock("slow", slow)
    loader.create({"id": "slow", "name": "slow"})
    reader_component = SimpleNamespace(
        inject=["loader"], apply=lambda ctx, config: None
    )
    reader = root.intercept("loader", {"await": True}).use(reader_component)
    unaffected = root.use(reader_component)
    assert loader.busy()
    assert reader.state == FiberState.PENDING
    assert unaffected.state == FiberState.ACTIVE

    gate.resolve(None)
    loader.wait().result()

    assert reader.state == FiberState.ACTIVE


def test_a_move_to_a_group_with_the_same_scope_keeps_both_fibers(
    loader: RecordingLoader, worker: Mock
) -> None:
    group = {"id": "g", "name": GROUP, "group": True, "config": []}
    loader.read([SETTINGS, group, worker_entry(inject=["settings"])]).result()
    entry = loader.resolve("worker")
    outer, inner = entry.outer_fiber, entry.inner_fiber

    loader.update("worker", {}, "g").result()

    assert entry.parent is loader.resolve("g").subgroup
    assert (entry.outer_fiber, entry.inner_fiber) == (outer, inner)
    assert len(worker.calls) == 1
