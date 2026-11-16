"""Tests for `Context` derivation and the context-level effect entry point."""

from collections.abc import Callable

import pytest

from pycordis.context import Context


def test_extend_adds_meta_to_the_child_only(root: Context) -> None:
    child = root.extend(a=1, b=2)
    grandchild = child.extend(a=3)

    assert dict(root.meta) == {}
    assert dict(child.meta) == {"a": 1, "b": 2}
    assert dict(grandchild.meta) == {"a": 3, "b": 2}


def test_derived_contexts_share_root_fiber_and_scheduler(root: Context) -> None:
    child = root.extend(a=1).isolate("db").intercept("db", {"x": 1})

    assert child is not root
    assert child.root is root
    assert child.fiber is root.fiber
    assert child.scheduler is root.scheduler
    assert isinstance(child, Context)


def test_isolate_gives_a_new_realm_unless_a_label_is_given(root: Context) -> None:
    label = object()
    first = root.isolate("db")
    second = root.isolate("db")
    shared_a = root.isolate("db", label)
    shared_b = root.isolate("db", label)

    assert "db" not in root.isolates
    assert first.isolates["db"] is not second.isolates["db"]
    assert shared_a.isolates["db"] is shared_b.isolates["db"] is label


def test_isolate_and_intercept_read_the_nearest_context_that_has_the_name(
    root: Context,
) -> None:
    outer = root.isolate("a").isolate("b").intercept("a", 1)
    inner = outer.isolate("a").intercept("a", 2).intercept("b", 3)

    assert set(outer.isolates) == {"a", "b"}
    assert inner.isolates["a"] is not outer.isolates["a"]
    assert inner.isolates["b"] is outer.isolates["b"]
    assert outer.intercepts["a"] == 1
    assert dict(inner.intercepts) == {"a": 2, "b": 3}


def test_context_tables_are_read_only(root: Context) -> None:
    child = root.isolate("db").intercept("db", 1).extend(a=1)

    for table in (child.isolates, child.intercepts, child.meta):
        with pytest.raises(TypeError):
            table["x"] = 1  # type: ignore[index]


def test_effect_through_a_derived_context_belongs_to_the_shared_fiber(
    root: Context,
) -> None:
    root.isolate("db").effect(lambda: None, "derived")

    assert [meta.label for meta in root.fiber.get_effects()] == ["derived"]


@pytest.mark.parametrize(
    "derive",
    [
        lambda ctx: ctx.extend(a=1),
        lambda ctx: ctx.isolate("db"),
        lambda ctx: ctx.intercept("db", 1),
    ],
    ids=["extend", "isolate", "intercept"],
)
def test_a_context_derived_from_a_shadow_keeps_the_def_site_and_the_use_site(
    root: Context, derive: Callable[[Context], Context]
) -> None:
    def_site = root.extend(site="def")
    shadow = root.extend(site="use").with_shadow(def_site)

    child = derive(shadow)

    assert root.shadow is None and root.use_site is root
    assert child.shadow is def_site
    assert child.use_site.shadow is None
    assert child.use_site is not child
    # the table that the call changed belongs to the use site
    plain = derive(shadow.use_site)
    for table in ("isolates", "intercepts", "meta"):
        assert set(getattr(child.use_site, table)) == set(getattr(plain, table))


def test_a_shadow_acts_for_the_use_site_and_is_not_nested(root: Context) -> None:
    first, second = root.extend(site="first"), root.extend(site="second")

    shadow = root.extend(site="use").with_shadow(first).with_shadow(second)

    assert shadow.shadow is second
    assert shadow.use_site.shadow is None
    assert shadow.meta["site"] == "use"


def test_repr_names_the_fiber(root: Context) -> None:
    assert repr(root) == "Context <root>"
