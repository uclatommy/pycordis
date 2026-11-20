"""Change reports, moves between trees, and the options that the loader keeps."""

import logging
from typing import Any

import pytest
from loader_fakes import GROUP, RecordingLoader

from pycordis.context import Context
from pycordis.loader import EntryChange, EntryTree, Group


def group(*children: dict[str, Any], **options: Any) -> dict[str, Any]:
    return {"name": GROUP, "group": True, "config": list(children), **options}


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::create reports a created entry with its id assigned"  # noqa: E501
)
def test_create_reports_a_created_entry_with_its_id_assigned(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")

    identifier = loader.create({"name": "foo", "config": {"a": 1}}).result()
    loader.create(group()).result()

    assert len(loader.changes) == 2
    [change, _] = loader.changes
    assert change.id == identifier
    assert change.group is loader.root
    assert change.legacy is None
    assert change.options == {"id": identifier, "name": "foo", "config": {"a": 1}}
    assert change.options in loader.root.data


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::update reports the new options together with the previous ones"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::update in place does not report a move"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::move reports the target group"  # noqa: E501
)
def test_update_reports_the_previous_options_and_a_move_reports_its_source(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")
    identifier = loader.create({"name": "foo", "config": {"a": 1}}).result()
    target = loader.create(group()).result()
    loader.changes.clear()

    loader.update(identifier, {"config": {"a": 2}}).result()

    [change] = loader.changes
    assert change.id == identifier
    assert change.legacy == {"id": identifier, "name": "foo", "config": {"a": 1}}
    assert change.options == {"id": identifier, "name": "foo", "config": {"a": 2}}
    assert change.moved_from is None
    loader.changes.clear()

    loader.update(identifier, {}, target).result()

    [change] = loader.changes
    assert change.group is loader.resolve(target).subgroup
    assert change.moved_from is loader.root
    assert change.options == change.legacy


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::fiber.update from inside the component reports a config change"  # noqa: E501
)
def test_fiber_update_from_inside_the_component_reports_a_config_change(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")
    identifier = loader.create({"name": "foo", "config": {"a": 4}}).result()
    loader.changes.clear()

    loader.fiber(identifier).update({"a": 5})

    [change] = loader.changes
    assert change.legacy["config"] == {"a": 4}  # type: ignore[index]
    assert change.options["config"] == {"a": 5}  # type: ignore[index]


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::remove reports the removed entry"  # noqa: E501
)
def test_remove_reports_the_removed_entry(loader: RecordingLoader) -> None:
    loader.mock("foo")
    identifier = loader.create({"name": "foo"}).result()
    loader.changes.clear()

    loader.remove(identifier)

    [change] = loader.changes
    assert change.id == identifier
    assert change.options is None
    assert change.legacy["name"] == "foo"  # type: ignore[index]
    assert identifier not in loader.store


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::does not mistake a child fiber disposing itself for the entry (#19)"  # noqa: E501
)
def test_a_child_fiber_that_disposes_itself_does_not_disable_the_entry(
    loader: RecordingLoader,
) -> None:
    def parent(ctx: Context, config: Any) -> None:
        ctx.use(lambda ctx, config: None).dispose()

    loader.mock("parent", parent)

    identifier = loader.create({"name": "parent"}).result()

    loader.fiber(identifier)
    assert "disabled" not in loader.resolve(identifier).options
    assert [c for c in loader.changes if (c.options or {}).get("disabled")] == []


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.commit: change reports::does not report anything for reads"  # noqa: E501
)
def test_a_read_that_removes_an_entry_reports_nothing(loader: RecordingLoader) -> None:
    loader.mock("foo")
    loader.read([{"id": "x", "name": "foo"}, {"id": "g", **group()}]).result()

    loader.read([{"id": "g", **group()}]).result()

    assert loader.changes == []
    assert "x" not in loader.store


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryGroup.remove: file-driven moves::keeps an entry that another group adopted"  # noqa: E501
)
def test_an_entry_that_another_group_adopted_survives_the_removal_from_its_old_group(
    loader: RecordingLoader,
) -> None:
    foo = loader.mock("foo")
    loader.read([{"id": "x", "name": "foo"}, {"id": "g", **group()}]).result()
    assert len(foo.calls) == 1

    loader.read([{"id": "g", **group({"id": "x", "name": "foo"})}]).result()

    assert loader.resolve("x").parent is loader.resolve("g").subgroup
    assert loader.running(foo) == 1


class Subtree(EntryTree):
    """A component that holds a nested tree with one group, `g`."""

    changes: list[EntryChange]

    def __init__(self, ctx: Context, config: Any) -> None:
        super().__init__(ctx, import_function=self._import)
        self.changes = []

    def _import(self, name: str) -> object:
        return Group if name == GROUP else self.loader.modules[name]  # type: ignore[attr-defined]

    def commit(self, change: EntryChange) -> None:
        self.changes.append(change)

    async def cordis_init(self) -> Any:
        yield self.root.stop
        await self.root.update([{"id": "g", **group(config=[])}])


@pytest.fixture
def subtree(loader: RecordingLoader) -> tuple[str, Subtree]:
    loader.modules["tree"] = Subtree
    identifier = loader.create({"name": "tree"}).result()
    loader.wait().result()
    loader.changes.clear()
    tree = loader.resolve(identifier).subtree
    assert isinstance(tree, Subtree)
    return identifier, tree


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.update: cross-tree moves::reports a removal to the source tree and a creation to the target tree"  # noqa: E501
)
def test_a_move_to_another_tree_is_a_removal_and_a_creation(
    loader: RecordingLoader, subtree: tuple[str, Subtree]
) -> None:
    tree_id, tree = subtree
    loader.mock("foo")
    identifier = loader.create({"name": "foo", "config": {"a": 1}}).result()
    loader.changes.clear()

    loader.update(identifier, {"config": {"a": 2}}, f"{tree_id}:g").result()

    [removal] = loader.changes
    assert removal.group is loader.root
    assert removal.options is None
    assert removal.legacy == {"id": identifier, "name": "foo", "config": {"a": 1}}
    [creation] = tree.changes
    assert creation.group is tree.store["g"].subgroup
    assert creation.legacy is None
    assert creation.moved_from is None
    assert creation.options == {"id": identifier, "name": "foo", "config": {"a": 2}}


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.update: cross-tree moves::transfers the entry to the store of the target tree"  # noqa: E501
)
@pytest.mark.divergence("loader-cross-tree-rebuild")
def test_a_move_to_another_tree_loads_the_entry_again_under_the_target_tree(
    loader: RecordingLoader, subtree: tuple[str, Subtree]
) -> None:
    tree_id, tree = subtree
    foo = loader.mock("foo")
    identifier = loader.create({"name": "foo"}).result()
    entry = loader.resolve(identifier)
    old_fiber = loader.fiber(identifier)

    loader.update(identifier, {}, f"{tree_id}:g").result()
    loader.wait().result()

    assert identifier not in loader.store
    assert tree.store[identifier] is entry
    assert loader.resolve(f"{tree_id}:{identifier}") is entry
    assert entry.id == f"{tree_id}:{identifier}"
    assert loader.running(foo) == 1
    new_fiber = loader.fiber(f"{tree_id}:{identifier}")
    assert new_fiber is not old_fiber
    assert new_fiber.parent.fiber is tree.ctx.fiber
    assert len(foo.calls) == 2

    loader.remove(f"{tree_id}:{identifier}")
    loader.wait().result()
    assert loader.running(foo) == 0


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.update: cross-tree moves::transfers the members of a moved group"  # noqa: E501
)
@pytest.mark.divergence("loader-cross-tree-rebuild")
def test_a_moved_group_loads_its_members_again_under_the_target_tree(
    loader: RecordingLoader, subtree: tuple[str, Subtree]
) -> None:
    tree_id, tree = subtree
    foo = loader.mock("foo")
    moved = loader.create(group({"id": "member", "name": "foo"})).result()
    assert loader.running(foo) == 1

    loader.update(moved, {}, f"{tree_id}:g").result()
    loader.wait().result()

    assert moved not in loader.store
    assert "member" not in loader.store
    assert tree.store[moved].subgroup.tree is tree  # type: ignore[union-attr]
    assert tree.store["member"].id == f"{tree_id}:member"
    assert loader.fiber(f"{tree_id}:member").parent.fiber is tree.ctx.fiber
    assert loader.running(foo) == 1

    loader.remove(f"{tree_id}:{moved}")
    loader.wait().result()
    assert loader.running(foo) == 0


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree.update: cross-tree moves::rejects a move onto an id that the target tree already uses"  # noqa: E501
)
def test_a_move_onto_an_id_that_the_target_tree_uses_changes_nothing(
    loader: RecordingLoader, subtree: tuple[str, Subtree]
) -> None:
    tree_id, tree = subtree
    loader.mock("foo")
    identifier = loader.create({"id": "g", "name": "foo"}).result()
    entry = loader.resolve(identifier)
    loader.changes.clear()

    with pytest.raises(ValueError, match="entry g already exists in the target tree"):
        loader.update(identifier, {}, f"{tree_id}:g")

    assert loader.store[identifier] is entry
    assert entry.parent is loader.root
    assert entry.options in loader.root.data
    assert tree.store["g"].subgroup.data == []  # type: ignore[union-attr]
    assert loader.changes == []
    assert tree.changes == []


@pytest.mark.upstream(
    "loader/commit.spec.ts::EntryTree disposal::does not persist child disposal while the tree loses its loader"  # noqa: E501
)
@pytest.mark.parametrize("disposed", ["tree", "loader"])
def test_a_tree_that_is_disposed_does_not_disable_its_children(
    loader: RecordingLoader, disposed: str
) -> None:
    commits: list[EntryChange] = []
    trees: list[EntryTree] = []

    class Tree(EntryTree):
        def __init__(self, ctx: Context, config: Any) -> None:
            super().__init__(ctx, import_function=lambda name: lambda ctx, config: None)
            trees.append(self)

        def commit(self, change: EntryChange) -> None:
            commits.append(change)

        async def cordis_init(self) -> Any:
            yield self.root.stop
            await self.root.update([{"id": "child", "name": "child"}])

    tree_fiber = loader.ctx.use(Tree)
    options = trees[0].root.data[0]
    child = trees[0].store["child"]
    assert child.live_fiber() is not None

    (tree_fiber if disposed == "tree" else loader.ctx.fiber).dispose()

    assert child.live_fiber() is None
    assert "disabled" not in options
    assert commits == []


def _movable(loader: RecordingLoader) -> None:
    loader.mock("foo")
    loader.read(
        [
            {"id": "a", **group({"id": "x", "name": "foo"})},
            {"id": "b", **group()},
        ]
    ).result()
    loader.changes.clear()


@pytest.mark.contract("LOAD-OPTIONS")
@pytest.mark.parametrize(
    "scenario",
    [
        "disable-old-group",
        "disable-new-group",
        "rename-and-rebuild",
    ],
)
def test_the_options_of_an_entry_change_only_for_a_host_call(
    loader: RecordingLoader, scenario: str
) -> None:
    _movable(loader)
    loader.update("x", {}, "b").result()
    loader.changes.clear()
    expected = {"id": "x", "name": "foo"}

    if scenario == "disable-old-group":
        loader.update("a", {"disabled": True}).result()
    elif scenario == "disable-new-group":
        loader.update("b", {"disabled": True}).result()
    else:
        loader.mock("bar")
        loader.update("x", {"name": "bar"}).result()
        expected["name"] = "bar"
    loader.wait().result()

    assert loader.resolve("x").options == expected
    assert [change.id for change in loader.changes if change.id == "x"] == (
        ["x"] if scenario == "rename-and-rebuild" else []
    )


@pytest.mark.contract("LOAD-OPTIONS")
@pytest.mark.divergence("loader-move-dispose")
@pytest.mark.parametrize("target", ["b", None])
def test_removing_the_old_group_of_a_moved_entry_neither_disposes_nor_disables_it(
    loader: RecordingLoader, target: str | None
) -> None:
    _movable(loader)
    loader.update("x", {}, target).result()
    fiber = loader.fiber("x")
    loader.changes.clear()

    loader.remove("a")
    loader.wait().result()

    assert loader.fiber("x") is fiber
    assert loader.resolve("x").options == {"id": "x", "name": "foo"}
    assert [change.id for change in loader.changes] == ["a"]


@pytest.mark.contract("LOAD-OPTIONS")
def test_the_self_dispose_of_a_component_is_the_only_change_that_nobody_asked_for(
    loader: RecordingLoader,
) -> None:
    _movable(loader)
    loader.update("x", {}, "b").result()
    loader.changes.clear()

    loader.fiber("x").dispose()

    assert loader.resolve("x").options == {"id": "x", "name": "foo", "disabled": True}
    assert [change.id for change in loader.changes] == ["x"]


@pytest.mark.divergence("notification-errors")
def test_a_failed_save_is_logged_and_does_not_fail_the_update(
    loader: RecordingLoader,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader.mock("foo")
    identifier = loader.create({"name": "foo", "config": {"a": 4}}).result()
    error = OSError("disk full")

    def fail(change: EntryChange) -> None:
        raise error

    monkeypatch.setattr(loader, "commit", fail)
    fiber = loader.fiber(identifier)

    with caplog.at_level(logging.ERROR, logger="pycordis"):
        fiber.update({"a": 5}).result()

    assert fiber.config == {"a": 5}
    assert [r.exc_info and r.exc_info[1] for r in caplog.records] == [error]
