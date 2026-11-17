"""Entry options: what the loader accepts and rejects before it changes anything."""

from typing import Any

import pytest
from loader_fakes import GROUP, RecordingLoader

from pycordis.errors import Issue
from pycordis.fiber import FiberState
from pycordis.loader import EntryValidationError


def issues_of(loader: RecordingLoader, options: object) -> list[Issue]:
    """Return the issues that `create` reports for `options`, or an empty list."""
    try:
        loader.create(options).result()  # type: ignore[arg-type]
    except EntryValidationError as error:
        return list(error.issues)
    return []


@pytest.mark.upstream(
    "loader/validate.spec.ts::Loader: entry options validation::accept valid options"
)
@pytest.mark.parametrize(
    "options",
    [
        {"name": "foo"},
        {
            "id": "a",
            "name": "foo",
            "group": False,
            "disabled": True,
            "inject": ["bar"],
            "intercept": {"bar": {}},
            "isolate": {"bar": True, "baz": "label"},
            "config": {"any": "thing"},
        },
        {"name": "foo", "group": None, "disabled": None, "inject": None},
        {"name": "foo", "inject": {"bar": {"required": True}}},
        {"name": GROUP, "group": True, "config": []},
    ],
)
def test_valid_options_are_accepted(
    loader: RecordingLoader, options: dict[str, Any]
) -> None:
    assert issues_of(loader, options) == []


@pytest.mark.upstream(
    "loader/validate.spec.ts::Loader: entry options validation::reject a missing or invalid field"  # noqa: E501
)
@pytest.mark.parametrize(
    ("options", "message", "path"),
    [
        (None, "entry must be an object", ()),
        (["foo"], "entry must be an object", ()),
        ({}, "name is required", ("name",)),
        ({"id": 1, "name": "foo"}, "id must be a string", ("id",)),
        ({"id": "a:b", "name": "foo"}, 'id must not contain ":"', ("id",)),
        ({"name": "g", "group": True}, "config is required", ("config",)),
        (
            {"name": "g", "group": True, "config": {}},
            "config must be an array",
            ("config",),
        ),
        (
            {"name": "foo", "inject": ["bar", 1]},
            "inject.1 must be a string",
            ("inject", 1),
        ),
        (
            {"name": "foo", "inject": "bar"},
            "inject must be an array or an object",
            ("inject",),
        ),
        (
            {"name": "foo", "intercept": []},
            "intercept must be an object",
            ("intercept",),
        ),
        (
            {"name": "foo", "isolate": {"bar": False}},
            "isolate.bar must be true or a string",
            ("isolate", "bar"),
        ),
        (
            {"name": "foo", "disabled": "yes"},
            "disabled must be a boolean",
            ("disabled",),
        ),
    ],
)
def test_a_missing_or_invalid_field_is_one_issue(
    loader: RecordingLoader, options: object, message: str, path: tuple[Any, ...]
) -> None:
    assert issues_of(loader, options) == [Issue(message, path)]


@pytest.mark.upstream(
    "loader/validate.spec.ts::Loader: entry options validation::accept an expression only for disabled"  # noqa: E501
)
@pytest.mark.divergence("loader-expressions")
def test_a_dict_is_not_an_expression_and_no_field_accepts_it(
    loader: RecordingLoader,
) -> None:
    expression = {"__jsExpr": "true"}

    found = issues_of(
        loader,
        {
            "name": expression,
            "group": expression,
            "disabled": expression,
            "inject": {"ok": None},
            "intercept": 1,
            "isolate": 2,
        },
    )

    assert [issue.path for issue in found] == [
        ("name",),
        ("group",),
        ("disabled",),
        ("intercept",),
        ("isolate",),
    ]


@pytest.mark.upstream(
    "loader/validate.spec.ts::Loader: entry options validation::reject invalid options at runtime before they are committed"  # noqa: E501
)
def test_invalid_options_change_nothing_and_are_not_committed(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")

    with pytest.raises(
        EntryValidationError,
        match=r"invalid entry options:\n  - name must be a string \(at name\)",
    ):
        loader.create({"name": 1})
    assert loader.changes == []
    assert loader.root.data == []

    identifier = loader.create({"name": "foo"}).result()
    loader.changes.clear()
    with pytest.raises(EntryValidationError):
        loader.update(identifier, {"disabled": "yes"})

    assert loader.changes == []
    assert loader.resolve(identifier).options == {"id": identifier, "name": "foo"}
    assert loader.fiber(identifier).state == FiberState.ACTIVE


@pytest.mark.upstream(
    "loader/validate.spec.ts::Loader: entry options validation::check the entries of a group passed at runtime as a whole"  # noqa: E501
)
def test_the_entries_of_a_group_are_checked_before_anything_changes(
    loader: RecordingLoader,
) -> None:
    loader.mock("foo")
    config = [None, {"id": "child", "name": "foo"}, {"name": "foo", "inject": [1]}]

    with pytest.raises(EntryValidationError) as caught:
        loader.create({"name": GROUP, "group": True, "config": config})

    assert str(caught.value) == "\n".join(
        [
            "invalid entry options:",
            "  - entry must be an object (at config.0)",
            "  - inject.0 must be a string (at config.2.inject.0)",
        ]
    )
    assert loader.changes == []
    assert "child" not in loader.store

    identifier = loader.create({"name": GROUP, "group": True, "config": []}).result()
    loader.changes.clear()
    with pytest.raises(
        EntryValidationError, match=r"name is required \(at config.0.name\)"
    ):
        loader.update(identifier, {"config": [{"id": "child"}]})
    assert loader.changes == []
    assert loader.resolve(identifier).options["config"] == []


@pytest.mark.upstream(
    "loader/validate.spec.ts::Loader: entry options validation::unload an entry whose options turn invalid, and reload it once fixed"  # noqa: E501
)
def test_an_entry_whose_options_turn_invalid_unloads_and_loads_again_once_fixed(
    loader: RecordingLoader,
) -> None:
    foo = loader.mock("foo")
    loader.read([{"id": "a", "name": "foo"}, {"id": "b", "name": "foo"}]).result()
    fiber = loader.fiber("a")

    loader.read(
        [{"id": "a", "name": "foo", "group": "no"}, {"id": "b", "name": "foo"}, "oops"]
    ).result()

    assert "a" not in loader.store
    assert fiber.state == FiberState.DISPOSED
    assert loader.fiber("b").state == FiberState.ACTIVE
    # the disposal is not taken for a self-dispose, which writes `disabled`
    assert loader.changes == []

    loader.read([{"id": "a", "name": "foo"}, {"id": "b", "name": "foo"}]).result()
    assert loader.fiber("a").state == FiberState.ACTIVE
    assert len(foo.calls) == 3
