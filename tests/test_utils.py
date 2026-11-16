"""Tests for `DisposableList` and call-site notes."""

import pytest

from pycordis.disposables import DisposableList
from pycordis.errors import CallSite

a, b, c, d = object(), object(), object(), object()


@pytest.mark.upstream(
    "utils.spec.ts::DisposableList::iterates unshifted entries before pushed ones"
)
def test_iteration_gives_unshifted_entries_newest_first_then_pushed_entries() -> None:
    entries = DisposableList[object]()
    entries.push(a)
    entries.unshift(b)
    entries.push(c)
    entries.unshift(d)

    assert list(entries) == [d, b, a, c]
    assert len(entries) == 4


@pytest.mark.upstream(
    "utils.spec.ts::DisposableList::disposer removes its own entry after a later push"
)
@pytest.mark.upstream(
    "utils.spec.ts::DisposableList::disposer removes its own entry "
    "after a later unshift"
)
@pytest.mark.parametrize("add", ["push", "unshift"])
def test_disposer_removes_its_own_entry(add: str) -> None:
    entries = DisposableList[object]()
    insert = getattr(entries, add)
    dispose_a = insert(a)
    dispose_b = insert(b)

    dispose_a()
    assert list(entries) == [b]

    dispose_b()
    assert list(entries) == []


def test_disposer_called_twice_leaves_a_later_registration_of_the_same_value() -> None:
    entries = DisposableList[object]()
    dispose_first = entries.push(a)
    dispose_first()
    entries.push(a)

    dispose_first()

    assert list(entries) == [a]


@pytest.mark.upstream(
    "utils.spec.ts::DisposableList::delete removes the entry it was given"
)
@pytest.mark.upstream(
    "utils.spec.ts::DisposableList::delete removes an unshifted entry"
)
@pytest.mark.parametrize("add", ["push", "unshift"])
def test_delete_removes_the_given_entry_once(add: str) -> None:
    entries = DisposableList[object]()
    entries.push(c)
    getattr(entries, add)(a)

    assert entries.delete(a) is True
    assert list(entries) == [c]
    assert entries.delete(a) is False


def test_delete_removes_the_newest_entry_of_a_repeated_value() -> None:
    entries = DisposableList[object]()
    entries.push(a)
    entries.unshift(a)

    assert entries.delete(a) is True
    assert list(entries) == [a]


@pytest.mark.upstream(
    "utils.spec.ts::DisposableList::clear removes every entry and returns them in order"
)
def test_clear_returns_the_entries_in_iteration_order() -> None:
    entries = DisposableList[object]()
    dispose_a = entries.push(a)
    entries.unshift(b)

    assert entries.clear() == [b, a]
    assert list(entries) == []
    assert len(entries) == 0
    assert entries.delete(a) is False
    dispose_a()
    assert list(entries) == []


def test_iteration_is_a_snapshot() -> None:
    entries = DisposableList[object]()
    entries.push(a)
    seen = []
    for entry in entries:
        entries.push(b)
        seen.append(entry)

    assert seen == [a]
    assert list(entries) == [a, b]


def _start_work() -> CallSite:
    return CallSite.capture()


def test_call_site_note_names_the_function_that_started_the_work() -> None:
    site = _start_work()
    error = RuntimeError("later")

    site.annotate(error, "Started by")

    (note,) = error.__notes__
    assert note.startswith("Started by:\n")
    assert note.index("test_call_site_note_names") < note.index("_start_work")


def test_call_site_skip_and_limit_bound_the_recorded_frames() -> None:
    whole = CallSite.capture(limit=100)
    skipped = CallSite.capture(skip=1, limit=100)
    limited = CallSite.capture(limit=1)

    assert skipped.frames == whole.frames[1:]
    assert len(limited.frames) == 1
    assert (
        limited.frames[0][0].co_name
        == "test_call_site_skip_and_limit_bound_the_recorded_frames"
    )
