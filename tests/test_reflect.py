"""Services, accessors, and realms of the root context."""

import random
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import Recorder

from pycordis.context import Context
from pycordis.errors import CordisError
from pycordis.keys import Key
from pycordis.scheduler import ImmediateScheduler


@pytest.mark.upstream("reflect.spec.ts::Reflect::Context.is()")
def test_a_subclass_instance_is_a_context(scheduler: ImmediateScheduler) -> None:
    class SubContext(Context):
        pass

    assert isinstance(SubContext(scheduler), Context)


def test_a_provided_service_is_read_with_get_and_a_mixin(root: Context) -> None:
    root.mixin("foo", ["bar"])
    root.provide("foo")
    root.set("foo", SimpleNamespace(bar=1))

    assert root.get("foo") is not None
    assert root["bar"] == 1
    assert root.get("bar") is None


@pytest.mark.parametrize("spelling", ["foo", Key[int]("foo")])
def test_a_key_and_its_name_select_the_same_service(
    root: Context, spelling: Any
) -> None:
    root.provide(spelling, 1)

    assert root.get("foo") == root.get(Key[int]("foo")) == root[spelling] == 1
    root[spelling] = 2
    assert root.get("foo") == 2


def test_a_key_must_be_a_str_or_a_key(root: Context) -> None:
    with pytest.raises(TypeError):
        root[1]  # type: ignore[index]


def test_a_service_that_nobody_provides_reads_as_none(root: Context) -> None:
    assert root["missing"] is None
    assert root.get("missing") is None


def test_set_without_a_provider_is_an_error(root: Context) -> None:
    with pytest.raises(CordisError, match='cannot set property "foo" without provide'):
        root.set("foo", 0)
    with pytest.raises(CordisError, match='cannot set property "foo" without provide'):
        root["foo"] = 0


def test_a_second_provider_in_one_realm_is_an_error(root: Context) -> None:
    root.provide("foo", 1)

    with pytest.raises(
        CordisError, match='service "foo" has been registered at <root>'
    ):
        root.provide("foo", 2)

    assert [effect.label for effect in root.fiber.get_effects()] == [
        'ctx.provide("foo")'
    ]
    assert len(root.fiber.provisions) == 1
    assert root.get("foo") == 1


@pytest.mark.parametrize(
    ("first", "second", "message"),
    [
        ("accessor", "provide", 'property "foo" is already declared as accessor'),
        ("provide", "accessor", 'property "foo" is already declared as service'),
        ("accessor", "accessor", 'property "foo" is already declared as accessor'),
    ],
)
def test_a_property_name_has_one_kind(
    root: Context, first: str, second: str, message: str
) -> None:
    def declare(kind: str) -> None:
        if kind == "provide":
            root.provide("foo")
        else:
            root.accessor("foo", lambda ctx: 1)

    declare(first)
    with pytest.raises(CordisError, match=message):
        declare(second)


def test_the_provider_value_is_visible_only_while_the_fiber_is_active(
    root: Context,
) -> None:
    seen: list[tuple[object, object, object]] = []
    root.provide("foo", 1)
    root.effect(
        lambda: (
            lambda: seen.append(
                (root.get("foo"), root.get("foo", strict=False), root["foo"])
            )
        )
    )

    root.fiber.dispose()

    assert seen == [(None, 1, 1)]


@pytest.mark.parametrize("on_removal", [False, True])
def test_a_provision_reports_its_change_to_service_listeners(
    root: Context, on_removal: bool
) -> None:
    heard = Recorder()
    root.on("internal/service", heard)
    handle = root.provide("foo", 7)
    if on_removal:
        handle()

    assert heard.calls == [("foo", 7)] + ([("foo", None)] if on_removal else [])


@pytest.mark.parametrize("label_shared", [False, True])
def test_contexts_that_share_a_realm_share_the_provider(
    root: Context, label_shared: bool
) -> None:
    first = root.isolate("foo", "realm")
    second = root.isolate("foo", "realm" if label_shared else None)
    first.provide("foo", 1)

    assert second.get("foo") == (1 if label_shared else None)
    assert root.get("foo") is None


def test_an_extended_context_sees_the_provider_of_its_parent(root: Context) -> None:
    root.provide("foo", 1)

    assert root.extend(tag=1).get("foo") == 1


def test_an_isolated_context_can_provide_beside_the_default_realm(
    root: Context,
) -> None:
    root.provide("foo", 1)
    isolated = root.isolate("foo")
    isolated.provide("foo", 2)

    assert (root.get("foo"), isolated.get("foo")) == (1, 2)


def test_service_listeners_hear_only_their_own_realm(root: Context) -> None:
    isolated = root.isolate("foo")
    default_heard, isolated_heard = Recorder(), Recorder()
    root.on("internal/service", default_heard)
    isolated.on("internal/service", isolated_heard)

    isolated.provide("foo", 2)

    assert default_heard.calls == []
    assert isolated_heard.calls == [("foo", 2)]


def test_a_removed_provider_waits_for_its_dependents(root: Context) -> None:
    handle = root.provide("foo", 1)
    (impl,) = root.fiber.provisions
    dependent = object()
    impl.dependents.add(dependent)

    result = handle()

    assert result is not None
    assert not result.done()
    assert root.get("foo", strict=False) is None
    assert impl in root.fiber.provisions
    assert impl.release is not None
    impl.release()
    assert result.done()
    assert not root.fiber.provisions


def test_a_removed_provider_without_dependents_leaves_at_once(root: Context) -> None:
    result = root.provide("foo", 1)()

    assert result is not None
    assert result.done()
    assert not root.fiber.provisions


def test_an_accessor_computes_reads_and_writes(root: Context) -> None:
    store = {"value": 1}
    handle = root.accessor(
        "answer",
        lambda ctx: store["value"],
        lambda ctx, value: store.update(value=value),
    )

    assert root["answer"] == 1
    root["answer"] = 5
    assert store == {"value": 5}
    handle()
    assert root["answer"] is None


def test_an_accessor_without_a_setter_is_read_only(root: Context) -> None:
    root.accessor("answer", lambda ctx: 42)

    with pytest.raises(CordisError, match='property "answer" is read-only'):
        root["answer"] = 0


@pytest.mark.parametrize(
    ("mixins", "name"),
    [(["bar"], "bar"), ({"bar": "alias"}, "alias")],
)
def test_a_mixin_forwards_to_the_service_and_is_reverted(
    root: Context, mixins: Any, name: str
) -> None:
    service = SimpleNamespace(bar=1)
    root.provide("foo", service)
    handle = root.mixin("foo", mixins)

    assert root[name] == 1
    root[name] = 2
    assert service.bar == 2
    handle()
    assert root[name] is None


def test_a_mixin_gives_none_while_the_service_is_absent(root: Context) -> None:
    root.mixin("foo", ["bar"])

    assert root["bar"] is None


def test_a_mixin_is_one_effect_with_one_child_per_attribute(root: Context) -> None:
    root.mixin("foo", ["a", "b"])

    (effect,) = root.fiber.get_effects()
    assert effect.label == 'ctx.mixin("foo")'
    assert [child.label for child in effect.children] == [
        'ctx.accessor("a")',
        'ctx.accessor("b")',
    ]


@pytest.mark.contract("EFF-REVERT")
@pytest.mark.parametrize("seed", range(20))
def test_reverting_every_provision_restores_the_provider_tables(
    root: Context, seed: int
) -> None:
    rng = random.Random(seed)
    names = ["a", "b", "c"]
    handles: dict[str, Any] = {}
    for _ in range(30):
        name = rng.choice(names)
        if name in handles and rng.random() < 0.5:
            handles.pop(name)()
        elif name not in handles:
            handles[name] = root.provide(name, rng.random())
    root.mixin("a", ["x"])

    root.fiber.dispose()

    assert [root.get(name, strict=False) for name in names] == [None] * 3, seed
    assert not root.fiber.provisions, seed
    assert root.fiber.get_effects() == [], seed
    for name in names:
        root.provide(name)
