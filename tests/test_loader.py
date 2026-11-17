"""The `Loader`: entries that load, update, fail, and dispose themselves."""

import logging
from typing import Any

import pytest
from loader_fakes import GROUP, Mock, RecordingLoader

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import FiberState
from pycordis.loader import EntryChange, EntryTree, Group, Loader, LoaderConfig
from pycordis.scheduler import ImmediateScheduler, spawn


def silent_update(ctx: Context, config: Any) -> None:
    """A component that takes every update itself, so that it keeps running."""
    ctx.on("internal/update", lambda fiber, config, next_: None)


@pytest.mark.upstream("loader/index.spec.ts::Loader: basic support::loader initiate")
@pytest.mark.upstream("loader/index.spec.ts::Loader: basic support::loader update")
def test_new_data_removes_the_entries_that_it_leaves_out_and_adopts_an_entry_that_moved(
    loader: RecordingLoader,
) -> None:
    foo = loader.mock("foo", silent_update)
    bar = loader.mock("bar", silent_update)
    qux = loader.mock("qux", silent_update)
    loader.read(
        [
            {"id": "1", "name": "foo"},
            {
                "id": "2",
                "name": GROUP,
                "config": [
                    {"id": "3", "name": "bar", "config": {"a": 1}},
                    {"id": "4", "name": "qux", "disabled": True},
                ],
            },
        ]
    ).result()
    assert (loader.running(foo), loader.running(bar), loader.running(qux)) == (1, 1, 0)
    assert (len(foo.calls), bar.calls, len(qux.calls)) == (1, [{"a": 1}], 0)
    for mock in (foo, bar, qux):
        mock.reset()

    loader.read([{"id": "1", "name": "foo"}, {"id": "4", "name": "qux"}]).result()

    assert (loader.running(foo), loader.running(bar), loader.running(qux)) == (1, 0, 1)
    assert (len(foo.calls), len(bar.calls), len(qux.calls)) == (0, 0, 1)
    assert [entry.id for entry in loader.entries()] == ["1", "4"]
    assert loader.changes == []


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: basic support::component self-update"
)
def test_a_component_that_updates_its_own_fiber_changes_the_data(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo", silent_update)
    loader.read([{"id": "1", "name": "foo"}, {"id": "4", "name": "foo"}]).result()

    loader.fiber("1").update({"a": 3})

    assert loader.data == [
        {"id": "1", "name": "foo", "config": {"a": 3}},
        {"id": "4", "name": "foo"},
    ]


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: basic support::component self-dispose"
)
def test_a_component_that_disposes_its_own_fiber_disables_its_entry(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo", silent_update)
    loader.read([{"id": "1", "name": "foo", "config": {"a": 3}}]).result()

    loader.fiber("1").dispose()

    assert loader.data == [
        {"id": "1", "name": "foo", "disabled": True, "config": {"a": 3}}
    ]
    assert [change.id for change in loader.changes] == ["1"]
    assert loader.resolve("1").disabled


@pytest.mark.upstream("loader/index.spec.ts::Loader: entry failure::on load")
@pytest.mark.upstream("loader/index.spec.ts::Loader: entry failure::on config update")
def test_a_failing_entry_does_not_stop_its_siblings(loader: RecordingLoader) -> None:
    def boom(ctx: Context, config: Any) -> None:
        raise RuntimeError("boom")

    loader.mock("bad", boom)
    loader.mock("good")

    loader.read([{"id": "1", "name": "bad"}, {"id": "2", "name": "good"}]).result()
    assert loader.fiber("1").state == FiberState.FAILED
    assert loader.fiber("2").state == FiberState.ACTIVE

    loader.read(
        [{"id": "1", "name": "bad", "config": {"a": 1}}, {"id": "2", "name": "good"}]
    ).result()
    assert loader.fiber("1").state == FiberState.FAILED
    assert loader.fiber("2").state == FiberState.ACTIVE


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: entry failure::on an id containing the separator"
)
def test_an_id_that_contains_the_separator_is_skipped(
    loader: RecordingLoader, caplog: pytest.LogCaptureFixture
) -> None:
    loader.mock("good")

    loader.read([{"id": "2", "name": "good"}, {"id": "3:4", "name": "good"}]).result()

    assert "3:4" not in loader.store
    assert loader.fiber("2").state == FiberState.ACTIVE
    assert 'id must not contain ":"' in caplog.text


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: failed entry recovery::fail with dependencies available"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: failed entry recovery::recover through update"
)
def test_a_failed_entry_loads_again_with_a_new_config(loader: RecordingLoader) -> None:
    failing = True

    def consume(ctx: Context, config: Any) -> None:
        if failing:
            raise RuntimeError("boom")

    consumer = loader.mock("consumer", consume)
    consumer.inject = ["foo"]  # type: ignore[attr-defined]
    loader.mock("provider", lambda ctx, config: ctx.provide("foo", {"value": 1}))
    loader.read(
        [
            {"id": "1", "name": "consumer", "config": {"value": 1}},
            {"id": "2", "name": "provider"},
        ]
    ).result()
    assert loader.fiber("1").state == FiberState.FAILED
    assert loader.fiber("1").committed is None

    failing = False
    loader.update("1", {"config": {"value": 1, "fixed": True}}).result()

    assert loader.fiber("1").state == FiberState.ACTIVE
    assert consumer.calls[1] == {"value": 1, "fixed": True}


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: nested component config::skip nested components"
)
def test_a_component_that_the_entry_component_uses_gets_its_config_unchanged(
    loader: RecordingLoader,
) -> None:
    child = loader.mock("child")
    config = {"value": {"nested": [1]}}
    loader.mock("parent", lambda ctx, cfg: ctx.use(child, config))

    loader.read([{"id": "1", "name": "parent"}]).result()

    assert len(child.calls) == 1
    assert child.calls[0] is config


def test_the_loader_links_only_the_fiber_of_the_entry_component(
    loader: RecordingLoader,
) -> None:
    nested = []

    def parent(ctx: Context, config: Any) -> None:
        nested.append(ctx.use(lambda ctx, config: None))

    loader.mock("parent", parent)
    loader.read([{"id": "1", "name": "parent"}]).result()

    fiber = loader.fiber("1")
    assert loader.entry_of(fiber) is loader.resolve("1")
    assert loader.entry_of(nested[0]) is None
    assert loader.locate(nested[0]) == "1"
    assert loader.locate() is None
    fiber.dispose()
    assert loader.entry_of(fiber) is None


def test_wait_completes_when_an_import_finishes(loader: RecordingLoader) -> None:
    gate = Deferred[None]()
    loader.gate = lambda name: gate
    foo = loader.mock("foo")

    read = loader.read([{"id": "1", "name": "foo"}])
    assert not read.done()
    assert loader.running(foo) == 0

    gate.resolve(None)

    read.result()
    assert loader.running(foo) == 1


def test_the_loader_copies_the_data_of_the_host(loader: RecordingLoader) -> None:
    loader.mock("foo")
    child = {"id": "c", "name": "foo"}
    group = {"id": "g", "name": GROUP, "group": True, "config": [child]}
    entry = {"id": "e", "name": "foo", "config": {"a": 1}}

    loader.read([group, entry]).result()
    child["disabled"] = True
    group["config"].append({"id": "x", "name": "foo"})  # type: ignore[attr-defined]
    entry["name"] = "other"

    assert loader.resolve("c").options == {"id": "c", "name": "foo"}
    assert [item["id"] for item in loader.root.data] == ["g", "e"]
    assert [item["id"] for item in loader.resolve("g").options["config"]] == ["c"]
    assert loader.resolve("e").options["name"] == "foo"


def test_the_options_of_a_change_report_are_not_the_dicts_of_the_loader(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")
    options = {"name": "foo", "config": {"a": 1}}
    identifier = loader.create(options).result()
    options["config"] = {"a": 9}  # type: ignore[assignment]
    [change] = loader.changes

    assert change.options is not loader.resolve(identifier).options
    assert change.options == {"id": identifier, "name": "foo", "config": {"a": 1}}
    change.options["name"] = "changed"  # type: ignore[index]
    assert loader.resolve(identifier).options["name"] == "foo"


def test_the_loader_logs_the_actions_of_a_component_when_logs_are_enabled(
    root: Context, caplog: pytest.LogCaptureFixture
) -> None:
    root.use(RecordingLoader, LoaderConfig(enable_logs=True))
    loader: Any = root.get("loader")
    loader.mock("foo", silent_update)

    with caplog.at_level(logging.INFO, logger="pycordis.loader"):
        loader.read(
            [
                {"id": "1", "name": "foo"},
                {"id": "g", "name": GROUP, "group": True, "config": []},
            ]
        ).result()
        loader.update("1", {"config": {"a": 1}}).result()
        loader.remove("1")

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "apply component foo",
        "reload component foo",
        "unload component foo",
    ]


def test_a_loader_is_the_service_loader(root: Context) -> None:
    root.use(Loader, LoaderConfig(package="p"))

    loader = root.get("loader")

    assert isinstance(loader, Loader)
    assert loader.config.package == "p"


def test_a_fiber_that_is_not_an_entry_component_is_not_reported(
    loader: RecordingLoader,
) -> None:
    child = loader.ctx.use(lambda ctx, config: None)

    child.update({"a": 1})

    assert loader.changes == []


def test_the_name_of_an_entry_can_change_while_its_module_is_imported(
    loader: RecordingLoader,
) -> None:
    foo, bar = loader.mock("foo"), loader.mock("bar")
    gate = Deferred[None]()
    loader.gate = lambda name: gate if name == "foo" else None
    created = loader.create({"id": "1", "name": "foo"})

    loader.update("1", {"name": "bar"})
    gate.resolve(None)

    created.result()
    assert (len(foo.calls), len(bar.calls)) == (0, 1)
    assert loader.running(bar) == 1


def test_loading_an_entry_that_is_loaded_does_not_create_a_second_fiber(
    root: Context, loader: RecordingLoader
) -> None:
    foo = loader.mock("foo")
    loader.create({"id": "1", "name": "foo"}).result()

    spawn(root.scheduler, loader.resolve("1").init()).result()

    assert len(foo.calls) == 1
    assert loader.running(foo) == 1


@pytest.mark.parametrize("identifier", ["missing", "1:missing", "missing:1"])
def test_an_unknown_entry_cannot_be_resolved(
    loader: RecordingLoader, identifier: str
) -> None:
    loader.mock("foo")
    loader.create({"id": "1", "name": "foo"}).result()

    with pytest.raises(ValueError, match=f"cannot resolve entry {identifier}"):
        loader.resolve(identifier)


def test_an_entry_that_is_not_a_group_cannot_be_a_parent(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")
    loader.create({"id": "1", "name": "foo"}).result()

    with pytest.raises(ValueError, match="entry 1 is not a group"):
        loader.create({"name": "foo"}, "1")


def test_a_group_needs_a_loader_and_an_entry(loader: RecordingLoader) -> None:
    loose = loader.ctx.use(Group, [])
    loader.wait().result()

    assert isinstance(loose.error, CordisError)
    assert "Group must run as the component of an entry" in str(loose.error)


def test_a_group_without_a_loader_fails_to_load() -> None:
    bare = Context(ImmediateScheduler())

    fiber = bare.use(Group, [])

    assert isinstance(fiber.error, CordisError)
    assert "the loader service is not provided" in str(fiber.error)


def test_an_entry_without_an_id_gets_a_random_hex_id_that_is_not_in_use(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")

    ids = [loader.create({"name": "foo"}).result() for _ in range(5)]

    assert len(set(ids)) == 5
    assert all(len(i) == 8 and int(i, 16) >= 0 for i in ids)


def test_create_and_update_place_an_entry_at_a_position_of_a_group(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")
    loader.read([{"id": "a", "name": "foo"}, {"id": "b", "name": "foo"}]).result()

    loader.create({"id": "c", "name": "foo"}, position=1).result()
    assert [item["id"] for item in loader.root.data] == ["a", "c", "b"]

    loader.update("a", {}, None, 2).result()
    assert [item["id"] for item in loader.root.data] == ["c", "b", "a"]


def test_an_update_disables_enables_and_deletes_fields_of_an_entry(
    loader: RecordingLoader,
) -> None:
    foo = loader.mock("foo")
    loader.create({"id": "1", "name": "foo", "config": {"a": 1}}).result()

    loader.update("1", {"disabled": True}).result()
    assert loader.running(foo) == 0
    assert loader.resolve("1").options["disabled"] is True

    loader.update("1", {"disabled": None}).result()
    assert loader.running(foo) == 1
    assert "disabled" not in loader.resolve("1").options

    loader.update("1", {"config": None}).result()
    assert loader.resolve("1").options == {"id": "1", "name": "foo"}
    assert foo.calls == [{"a": 1}, {"a": 1}, None]


@pytest.mark.upstream("loader/index.spec.ts::Loader: intercept config::pending")
@pytest.mark.upstream("loader/index.spec.ts::Loader: intercept config::resolved")
def test_a_reader_that_sets_await_uses_the_loader_when_no_entry_is_busy(
    loader: RecordingLoader,
) -> None:
    gate = Deferred[None]()

    async def slow(ctx: Context, config: Any) -> None:
        await gate

    loader.mock("foo", slow)
    loader.mock("bar", silent_update, inject=["never"])
    loader.mock("qux")

    loader.create({"id": "foo", "name": "foo"})
    loader.create({"id": "bar", "name": "bar"})
    loader.create({"id": "qux", "name": "qux", "inject": {"loader": {"await": True}}})

    assert loader.fiber("foo").state == FiberState.LOADING
    assert loader.fiber("bar").state == FiberState.PENDING
    qux = loader.resolve("qux")
    assert qux.outer_fiber is not None
    assert qux.outer_fiber.state == FiberState.PENDING
    assert qux.inner_fiber is None

    gate.resolve(None)

    assert loader.fiber("foo").state == FiberState.ACTIVE
    assert loader.fiber("bar").state == FiberState.PENDING
    assert loader.fiber("qux").state == FiberState.ACTIVE


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: pending entry update::persist an update to a pending fiber"  # noqa: E501
)
def test_an_update_of_a_fiber_that_waits_for_a_service_changes_the_data(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo", inject=["never"])
    loader.read([{"id": "1", "name": "foo"}]).result()
    assert loader.fiber("1").state == FiberState.PENDING

    loader.fiber("1").update({"a": 3})

    assert loader.data == [{"id": "1", "name": "foo", "config": {"a": 3}}]


def nested_tree(loader: RecordingLoader, changes: list[EntryChange]) -> type[EntryTree]:
    """Return a component that holds a tree and stops its children before it leaves."""

    class NestedTree(EntryTree):
        inject = ["foo"]

        def __init__(self, ctx: Context, config: Any) -> None:
            super().__init__(ctx, import_function=loader.import_function)

        def commit(self, change: EntryChange) -> None:
            changes.append(change)

        async def cordis_init(self) -> Any:
            def stop() -> Deferred[None]:
                async def later() -> None:
                    # the children are disposed before they are unregistered
                    await Deferred.resolved(None)
                    self.root.stop()

                return spawn(self.ctx.scheduler, later())

            yield stop
            await self.root.update([{"id": "a", "name": "bar"}])  # type: ignore[list-item]

    return NestedTree


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: parent teardown::keep children enabled when the parent loses a dependency"  # noqa: E501
)
def test_the_children_of_a_tree_that_loses_a_service_keep_their_options(
    root: Context, loader: RecordingLoader
) -> None:
    loader.mock("bar")
    changes: list[EntryChange] = []
    loader.modules["tree"] = nested_tree(loader, changes)
    dispose = root.provide("foo", {})
    loader.read([{"id": "1", "name": "tree"}]).result()
    assert loader.fiber("1").state == FiberState.ACTIVE
    assert loader.resolve("1:a").live_fiber() is not None

    dispose()
    loader.wait().result()

    assert loader.fiber("1").state == FiberState.PENDING
    assert changes == []


@pytest.mark.upstream(
    "loader/index.spec.ts::Loader: replaced component::keeps the entry of a component replaced in place"  # noqa: E501
)
def test_a_component_that_the_registry_replaces_keeps_its_entry(
    root: Context, loader: RecordingLoader
) -> None:
    foo = loader.mock("foo")
    loader.read([{"id": "1", "name": "foo", "config": {"a": 1}}]).result()
    fiber = loader.fiber("1")
    replacement = Mock("next")

    root.registry.replace(foo, replacement)
    fiber.wait().result()

    assert loader.fiber("1") is fiber
    assert replacement.calls == [{"a": 1}]
    assert not loader.resolve("1").disabled
    assert loader.root.data == [{"id": "1", "name": "foo", "config": {"a": 1}}]
