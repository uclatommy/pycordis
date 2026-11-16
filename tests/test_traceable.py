"""Views: what `get_traceable` returns and how a view reads and writes."""

from typing import Any

import pytest

from pycordis.context import Context
from pycordis.service import Service
from pycordis.traceable import Tracker, get_traceable


class Box:
    cordis_tracker = Tracker(property="ctx")

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.size = 1

    @property
    def doubled(self) -> int:
        return self.size * 2

    @doubled.setter
    def doubled(self, value: int) -> None:
        self.size = value // 2


@pytest.mark.parametrize("value", [None, 1, "text", [], object()])
def test_a_value_without_a_tracker_is_returned_as_it_is(
    root: Context, value: Any
) -> None:
    assert get_traceable(root, value) is value


def test_a_view_reads_and_writes_the_original_and_reads_the_use_site(
    root: Context,
) -> None:
    box = Box(root)
    use_site = root.extend(name="use")
    view = get_traceable(use_site, box)

    assert isinstance(view, Box)
    assert view.cordis_original is box
    assert view.ctx is use_site
    view.size = 5
    assert (box.size, view.doubled) == (5, 10)
    view.doubled = 8
    assert box.size == 4
    assert repr(view) == repr(box)
    # a hook name is read and written on the original
    view.cordis_note = "x"
    assert box.cordis_note == "x"  # type: ignore[attr-defined]
    del view.size
    assert not hasattr(box, "size")


def test_a_property_without_a_setter_is_read_only_on_a_view(root: Context) -> None:
    class Readonly(Box):
        @property
        def label(self) -> str:
            return "x"

    view = get_traceable(root, Readonly(root))

    assert view.label == "x"
    with pytest.raises(AttributeError, match="no setter"):
        view.label = "y"


def test_a_view_traced_again_shows_the_original_for_the_new_use_site(
    root: Context,
) -> None:
    box = Box(root)
    first, second = root.extend(a=1), root.extend(a=2)

    view = get_traceable(second, get_traceable(first, box))

    assert view.cordis_original is box
    assert view.ctx is second


@pytest.mark.parametrize("name", ["ctx", "cordis_original", "cordis_caller"])
def test_a_view_does_not_accept_a_write_to_its_own_names(
    root: Context, name: str
) -> None:
    view = get_traceable(root, Box(root))

    with pytest.raises(AttributeError, match="traceable view"):
        setattr(view, name, 1)


def test_a_method_cannot_replace_the_context_that_it_sees(root: Context) -> None:
    class Replacer(Service[Any]):
        def __init__(self, ctx: Context, config: object = None) -> None:
            super().__init__(ctx, "replacer")

        def replace(self) -> None:
            self.ctx = root  # type: ignore[assignment]

    root.use(Replacer)

    with pytest.raises(AttributeError, match="traceable view"):
        root["replacer"].replace()


def test_a_callable_view_without_a_hook_calls_the_object(root: Context) -> None:
    class Adder(Box):
        def __call__(self, extra: int) -> int:
            return self.size + extra

    assert get_traceable(root, Adder(root))(2) == 3
