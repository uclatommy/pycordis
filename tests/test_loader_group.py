"""Groups: enable and disable, transfer, awaited reconciliation, and fiber ownership."""

from typing import Any

import pytest
from loader_fakes import GROUP, RecordingLoader

from pycordis.deferred import Deferred


def group(*children: dict[str, Any], **options: Any) -> dict[str, Any]:
    return {"name": GROUP, "group": True, "config": list(children), **options}


class Counter:
    """A component `foo` and the number of its disposals."""

    def __init__(self, loader: RecordingLoader) -> None:
        self.disposed = 0
        self.foo = loader.mock("foo", lambda ctx, config: self._dispose)

    def _dispose(self) -> None:
        self.disposed += 1

    def reset(self) -> None:
        self.foo.reset()
        self.disposed = 0

    def seen(self) -> tuple[int, int]:
        """The calls of the component and its disposals since the last reset."""
        return len(self.foo.calls), self.disposed


@pytest.fixture
def nested(loader: RecordingLoader) -> tuple[Counter, str, str]:
    counter = Counter(loader)
    outer = loader.create(group({"name": "foo"})).result()
    inner = loader.create(group({"name": "foo"}), outer).result()
    loader.wait().result()
    assert counter.seen() == (2, 0)
    assert len(list(loader.entries())) == 4
    counter.reset()
    return counter, outer, inner


@pytest.mark.upstream("loader/group.spec.ts::Group: basic support::initialize")
@pytest.mark.upstream("loader/group.spec.ts::Group: basic support::disable inner")
@pytest.mark.upstream("loader/group.spec.ts::Group: basic support::disable outer")
@pytest.mark.upstream("loader/group.spec.ts::Group: basic support::enable inner")
@pytest.mark.upstream("loader/group.spec.ts::Group: basic support::enable outer")
def test_a_disabled_group_unloads_its_children_and_enabling_it_loads_them_again(
    loader: RecordingLoader, nested: tuple[Counter, str, str]
) -> None:
    counter, outer, inner = nested

    loader.update(inner, {"disabled": True}).result()
    assert counter.seen() == (0, 1)
    child = loader.resolve(inner).subgroup.data[0]["id"]  # type: ignore[union-attr]
    assert not loader.resolve(inner).disabled  # a group is never disabled
    assert loader.resolve(child).disabled
    assert len(list(loader.entries())) == 4

    counter.reset()
    loader.update(outer, {"disabled": True}).result()
    assert counter.seen() == (0, 1)

    counter.reset()
    loader.update(inner, {"disabled": None}).result()
    assert counter.seen() == (0, 0)  # the outer group is still disabled

    loader.update(outer, {"disabled": None}).result()
    assert counter.seen() == (2, 0)
    assert len(list(loader.entries())) == 4


@pytest.mark.upstream("loader/group.spec.ts::Group: transfer::initialize")
@pytest.mark.upstream("loader/group.spec.ts::Group: transfer::enabled -> enabled")
@pytest.mark.upstream("loader/group.spec.ts::Group: transfer::enabled -> disabled")
@pytest.mark.upstream("loader/group.spec.ts::Group: transfer::disabled -> disabled")
@pytest.mark.upstream("loader/group.spec.ts::Group: transfer::disabled -> enabled")
def test_a_moved_entry_loads_or_unloads_by_the_verdict_of_its_new_group(
    loader: RecordingLoader,
) -> None:
    counter = Counter(loader)
    entry = loader.create({"name": "foo"}).result()
    alpha = loader.create(group()).result()
    beta = loader.create(group(disabled=True), alpha).result()
    gamma = loader.create(group(), beta).result()
    assert counter.seen() == (1, 0)
    assert len(list(loader.entries())) == 4

    steps = [
        (alpha, (0, 0)),  # enabled -> enabled
        (beta, (0, 1)),  # enabled -> disabled
        (gamma, (0, 0)),  # disabled -> disabled
        (None, (1, 0)),  # disabled -> enabled
    ]
    for target, expected in steps:
        counter.reset()
        loader.update(entry, {}, target).result()
        assert counter.seen() == expected
        assert len(list(loader.entries())) == 4


@pytest.mark.upstream("loader/group.spec.ts::Group: awaited reconciliation::initialize")
@pytest.mark.upstream(
    "loader/group.spec.ts::Group: awaited reconciliation::config update settles children"  # noqa: E501
)
def test_a_config_update_of_a_group_ends_when_its_new_children_have_loaded(
    loader: RecordingLoader,
) -> None:
    foo, bar = loader.mock("foo"), loader.mock("bar")
    identifier = loader.create(group({"id": "1", "name": "foo"})).result()
    assert (loader.running(foo), loader.running(bar)) == (1, 0)
    gate = Deferred[None]()
    loader.gate = lambda name: gate if name == "bar" else None

    update = loader.update(
        identifier,
        {"config": [{"id": "1", "name": "foo"}, {"id": "2", "name": "bar"}]},
    )
    assert not update.done()
    assert loader.running(bar) == 0
    gate.resolve(None)

    update.result()
    assert loader.running(bar) == 1
    assert loader.resolve("2").live_fiber() is not None


@pytest.mark.divergence("loader-tree-owner")
def test_the_fiber_of_an_entry_is_a_child_of_the_fiber_of_its_tree(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")
    loader.read(
        [{"id": "a", "name": "foo"}, {"id": "g", **group({"id": "b", "name": "foo"})}]
    ).result()

    for identifier in ("a", "b"):
        fiber = loader.fiber(identifier)
        assert fiber.parent.fiber is loader.ctx.fiber
        assert fiber in loader.ctx.fiber.children
    assert loader.fiber("g").parent.fiber is loader.ctx.fiber


@pytest.mark.divergence("loader-tree-owner")
@pytest.mark.parametrize("action", ["disable", "remove"])
def test_a_moved_entry_stays_loaded_when_its_old_group_goes_away(
    loader: RecordingLoader, action: str
) -> None:
    foo = loader.mock("foo")
    old = loader.create(group({"id": "x", "name": "foo"})).result()
    target = loader.create(group()).result()
    loader.update("x", {}, target).result()
    fiber = loader.fiber("x")
    loader.changes.clear()

    if action == "disable":
        loader.update(old, {"disabled": True}).result()
    else:
        loader.remove(old)
    loader.wait().result()

    assert loader.fiber("x") is fiber
    assert loader.running(foo) == 1
    assert loader.resolve("x").options == {"id": "x", "name": "foo"}
    assert [change.id for change in loader.changes] == [old]


def test_a_group_that_is_removed_removes_its_children(loader: RecordingLoader) -> None:
    foo = loader.mock("foo")
    identifier = loader.create(group({"id": "x", "name": "foo"})).result()

    loader.remove(identifier)
    loader.wait().result()

    assert loader.running(foo) == 0
    assert "x" not in loader.store


def test_a_move_inside_a_tree_keeps_the_fiber_of_the_entry(
    loader: RecordingLoader,
) -> None:
    foo = loader.mock("foo")
    target = loader.create(group()).result()
    entry = loader.create({"name": "foo"}).result()
    fiber = loader.fiber(entry)

    loader.update(entry, {}, target).result()

    assert loader.fiber(entry) is fiber
    assert len(foo.calls) == 1
