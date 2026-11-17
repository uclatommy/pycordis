"""Entry `isolate`: the realm of each service, and the rebuild when it changes."""

from typing import Any

import pytest
from loader_fakes import GROUP, Mock, RecordingLoader

from pycordis.context import Context
from pycordis.fiber import FiberState


def provide_bar(ctx: Context, config: Any) -> None:
    ctx.provide("bar", dict(config or {}))


class Probe:
    """Records the loads of an injector of `bar`, and the unloads of each load."""

    def __init__(self, loader: RecordingLoader) -> None:
        self.disposed = 0
        self.values: list[Any] = []
        self.foo: Mock = loader.mock("foo", self._load, inject=["bar"])
        loader.mock("bar", provide_bar)

    def _load(self, ctx: Context, config: Any) -> Any:
        self.values.append(ctx["bar"].get("value"))
        return self._unload

    def _unload(self) -> None:
        self.disposed += 1

    def reset(self) -> None:
        self.foo.reset()
        self.disposed = 0
        self.values.clear()

    def counts(self) -> tuple[int, int]:
        """Return the loads and the unloads since the last `reset`."""
        return len(self.foo.calls), self.disposed


def load_pair(
    loader: RecordingLoader, target: str, isolate: dict[str, Any] | None
) -> dict[str, str]:
    """Create the provider `bar` and the injector `foo`, and return their ids.

    `target` names the entry that gets `isolate`.
    """
    options: dict[str, dict[str, Any]] = {
        "foo": {"name": "foo"},
        "bar": {"name": "bar"},
    }
    if isolate is not None:
        options[target]["isolate"] = isolate
    return {name: loader.create(options[name]).result() for name in ("bar", "foo")}


@pytest.fixture
def probe(loader: RecordingLoader) -> Probe:
    return Probe(loader)


@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: basic::add isolate on injector (relevant)"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: basic::add isolate on injector (irrelevant)"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: basic::remove isolate on injector (relevant)"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: basic::add isolate on provider (relevant)"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: basic::add isolate on provider (irrelevant)"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: basic::remove isolate on provider (relevant)"  # noqa: E501
)
@pytest.mark.parametrize(
    ("target", "before", "after", "counts"),
    [
        ("foo", None, {"bar": True}, (0, 1)),
        ("foo", {"bar": True}, {"bar": True, "qux": True}, (0, 0)),
        ("foo", {"bar": True, "qux": True}, {"qux": True}, (1, 0)),
        ("bar", None, {"bar": True}, (0, 1)),
        ("bar", {"bar": True}, {"bar": True, "qux": True}, (0, 0)),
        ("bar", {"bar": True, "qux": True}, {"qux": True}, (1, 0)),
    ],
)
def test_an_isolate_change_that_changes_a_service_reloads_the_injector(
    loader: RecordingLoader,
    probe: Probe,
    target: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    counts: tuple[int, int],
) -> None:
    ids = load_pair(loader, target, before)
    probe.reset()

    loader.update(ids[target], {"isolate": after}).result()

    assert probe.counts() == counts


@pytest.mark.upstream("loader/isolate.spec.ts::Service Isolation: basic::initiate")
def test_an_injector_loads_with_a_provider_in_the_same_realm(
    loader: RecordingLoader, probe: Probe
) -> None:
    loader.create({"name": "bar"}).result()
    loader.create({"name": "foo"}).result()

    assert probe.counts() == (1, 0)


@pytest.mark.divergence("loader-isolate-rebuild")
@pytest.mark.parametrize("target", ["foo", "bar"])
def test_an_isolate_change_reloads_the_entry_also_when_no_service_changes(
    loader: RecordingLoader, probe: Probe, target: str
) -> None:
    ids = load_pair(loader, target, {"qux": True})
    probe.reset()
    before = loader.fiber(ids[target])

    loader.update(ids[target], {"isolate": None}).result()

    assert loader.fiber(ids[target]) is not before
    assert before.uid is None
    assert probe.counts() == (1, 1)
    assert loader.fiber(ids["foo"]).state == FiberState.ACTIVE


def group_of(*members: dict[str, Any], **options: Any) -> dict[str, Any]:
    return {"name": GROUP, "config": list(members), **options}


@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: realm::add isolate group"
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: realm::update isolate group (no change)"
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: realm::realm reference"
)
def test_entries_see_the_realm_of_their_group_and_their_own_isolate(
    loader: RecordingLoader, probe: Probe
) -> None:
    alpha = loader.create(
        group_of({"name": "bar", "config": {"value": "alpha"}}, isolate={"bar": True})
    ).result()
    loader.create(
        group_of({"name": "bar", "config": {"value": "beta"}}, isolate={"bar": "beta"})
    ).result()

    assert loader.running(loader.modules["bar"]) == 2
    assert probe.counts() == (0, 0)

    loader.update(alpha, {"isolate": {"bar": True}}).result()

    assert loader.running(loader.modules["bar"]) == 2
    assert probe.counts() == (0, 0)

    nested1 = loader.create({"name": "foo"}, alpha).result()
    nested2 = loader.create({"name": "foo", "isolate": {"bar": "beta"}}, alpha).result()
    nested3 = loader.create({"name": "foo", "isolate": {"bar": True}}, alpha).result()

    assert probe.values == ["alpha", "beta"]
    assert probe.counts() == (2, 0)
    assert loader.fiber(nested1).state == FiberState.ACTIVE
    assert loader.fiber(nested2).state == FiberState.ACTIVE
    assert loader.fiber(nested3).state == FiberState.PENDING


@pytest.mark.divergence("loader-isolate-rebuild")
@pytest.mark.parametrize(
    ("label", "other", "tag"), [("alpha", "beta", "beta"), ("beta", "alpha", "alpha")]
)
def test_a_group_isolate_change_rebuilds_its_members_in_the_new_realm(
    loader: RecordingLoader, probe: Probe, label: str, other: str, tag: str
) -> None:
    for name in (label, other):
        loader.create(
            {"name": "bar", "isolate": {"bar": name}, "config": {"value": name}}
        ).result()
    group = loader.create(group_of(isolate={"bar": label})).result()
    member = loader.create({"name": "foo"}, group).result()
    old = loader.fiber(member)
    assert probe.values == [label]
    probe.reset()

    loader.update(group, {"isolate": {"bar": other}}).result()

    assert old.uid is None
    assert probe.counts() == (1, 1)
    assert probe.values == [tag]
    assert loader.fiber(member).state == FiberState.ACTIVE


@pytest.mark.divergence("loader-isolate-rebuild")
def test_an_isolate_change_of_an_outer_group_rebuilds_the_nested_groups(
    loader: RecordingLoader, probe: Probe
) -> None:
    outer = loader.create(group_of()).result()
    inner = loader.create(group_of(isolate={"bar": "custom"}), outer).result()
    loader.create({"name": "bar", "config": {"value": "custom"}}, inner).result()
    loader.create({"name": "foo", "isolate": {"bar": "custom"}}).result()
    member = loader.create({"name": "foo"}, inner).result()
    assert probe.values == ["custom", "custom"]
    probe.reset()

    loader.update(outer, {"isolate": {"bar": "custom"}}).result()

    # the nested provider loads again, so its injector in the same realm loads again
    assert probe.counts() == (2, 2)
    assert loader.fiber(member).state == FiberState.ACTIVE


@pytest.mark.upstream("loader/isolate.spec.ts::Service Isolation: transfer::initiate")
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: transfer::transfer injector into group"
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: transfer::transfer provider into group"
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: transfer::transfer injector out of group"  # noqa: E501
)
@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: transfer::transfer provider out of group"  # noqa: E501
)
def test_a_move_into_or_out_of_an_isolating_group_changes_the_realm(
    loader: RecordingLoader, probe: Probe
) -> None:
    group = loader.create(group_of(isolate={"bar": True})).result()
    provider = loader.create({"name": "bar"}).result()
    injector = loader.create({"name": "foo"}).result()
    assert probe.counts() == (1, 0)

    for entry, target, counts in [
        (injector, group, (0, 1)),
        (provider, group, (1, 0)),
        (injector, None, (0, 1)),
        (provider, None, (1, 0)),
    ]:
        probe.reset()
        loader.update(entry, {}, target).result()
        assert probe.counts() == counts


@pytest.mark.upstream(
    "loader/isolate.spec.ts::Service Isolation: migration::keeps the committed views of a parent and a pinned child across a realm move"  # noqa: E501
)
def test_a_parent_and_its_pinned_child_unload_against_the_realm_they_loaded_in(
    loader: RecordingLoader,
) -> None:
    loads: list[str] = []
    unloads: list[str] = []

    def bar(ctx: Context, config: Any) -> None:
        ctx.provide("bar", {"tag": config["tag"]})

    def parent(ctx: Context, config: Any) -> Any:
        # the child is pinned to the realm in which the parent resolves `bar`
        pinned = ctx.isolate("bar", ctx.isolates["bar"])

        def child(inner: Context, config: Any) -> Any:
            loads.append("child:" + inner["bar"]["tag"])
            return lambda: unloads.append("child:" + inner["bar"]["tag"])

        child.inject = ["bar"]  # type: ignore[attr-defined]
        pinned.use(child)
        return lambda: unloads.append("parent:" + ctx["bar"]["tag"])

    loader.mock("bar", bar)
    loader.mock("parent", parent, inject=["bar"])
    for label in ("a", "b"):
        loader.create(
            {"name": "bar", "isolate": {"bar": label}, "config": {"tag": label}}
        ).result()
    entry = loader.create({"name": "parent", "isolate": {"bar": "a"}}).result()
    assert loads == ["child:a"]

    loader.update(entry, {"isolate": {"bar": "b"}}).result()

    assert loads == ["child:a", "child:b"]
    assert sorted(unloads) == ["child:a", "parent:a"]


@pytest.mark.divergence("loader-isolate-rebuild")
def test_a_provider_that_changes_realm_loads_again_and_leaves_its_old_realm(
    loader: RecordingLoader,
) -> None:
    calls: list[Any] = []

    def provider(ctx: Context, config: Any) -> None:
        calls.append(config)
        ctx.provide("svc", {"tag": config["tag"]})

    loader.mock("svc", provider)
    entry = loader.create({"name": "svc", "config": {"tag": "init"}}).result()
    old = loader.fiber(entry)
    assert loader.ctx.get("svc") == {"tag": "init"}

    loader.update(entry, {"isolate": {"svc": "A"}}).result()

    assert len(calls) == 2
    assert old.uid is None
    assert loader.ctx.get("svc", strict=False) is None
    realm = loader.fiber(entry).ctx.isolates["svc"]
    assert realm == "A"
    assert loader.fiber(entry).ctx["svc"] == {"tag": "init"}


@pytest.mark.divergence("loader-isolate-rebuild")
def test_a_provider_that_changes_realm_unloads_after_the_injectors_of_its_old_realm(
    loader: RecordingLoader,
) -> None:
    timeline: list[str] = []

    def provider(ctx: Context, config: Any) -> Any:
        ctx.provide("svc", {"tag": config["tag"]})
        return lambda: timeline.append(f"closed: {config['tag']}")

    def consumer(ctx: Context, config: Any) -> Any:
        return lambda: timeline.append(f"read: {ctx['svc']['tag']}")

    loader.mock("provider", provider)
    loader.mock("consumer", consumer, inject=["svc"])
    entry = loader.create({"name": "provider", "config": {"tag": "moved"}}).result()
    other = loader.create(
        {"name": "provider", "isolate": {"svc": "B"}, "config": {"tag": "other"}}
    ).result()
    reader = loader.create({"name": "consumer"}).result()
    assert loader.fiber(reader).state == FiberState.ACTIVE

    loader.update(entry, {"isolate": {"svc": "B"}}).result()

    # the injector in the old realm reads its provider before the provider closes
    assert timeline == ["read: moved", "closed: moved"]
    assert loader.fiber(reader).state == FiberState.PENDING
    assert loader.fiber(other).ctx["svc"] == {"tag": "other"}


def test_equal_isolate_labels_share_a_realm_when_they_are_different_strings(
    loader: RecordingLoader, probe: Probe
) -> None:
    labels = ["".join(["sha", "red"]), "".join(["sh", "ared"])]
    assert labels[0] == labels[1] and labels[0] is not labels[1]
    loader.create(
        {"name": "bar", "isolate": {"bar": labels[0]}, "config": {"value": "x"}}
    ).result()
    loader.create({"name": "foo", "isolate": {"bar": labels[1]}}).result()

    assert probe.values == ["x"]


@pytest.mark.divergence("loader-isolate-rebuild")
def test_a_move_into_a_group_with_other_intercepts_loads_the_component_again(
    loader: RecordingLoader,
) -> None:
    seen: list[Context] = []
    component = loader.mock("foo", lambda ctx, config: seen.append(ctx))
    groups = [
        loader.create(group_of(intercept={"foo": {"n": n}})).result() for n in (1, 2)
    ]
    entry = loader.create({"name": "foo"}, groups[0]).result()
    old = loader.fiber(entry)

    loader.update(entry, {}, groups[1]).result()

    assert old.uid is None
    assert len(component.calls) == 2
    assert seen[1].intercept_layers("foo") == [{"n": 2}]
