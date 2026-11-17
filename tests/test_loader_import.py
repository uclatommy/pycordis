"""The default import function: Python names, packages, and import errors."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from pycordis.context import Context
from pycordis.loader import Loader, LoaderConfig


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A directory on `sys.path` for the modules of one test, forgotten afterwards."""
    before = set(sys.modules)
    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    for name in set(sys.modules) - before:
        del sys.modules[name]


def start(root: Context, package: str | None = None) -> Loader:
    root.use(Loader, LoaderConfig(package=package))
    loader = root.get("loader")
    assert isinstance(loader, Loader)
    return loader


def write(source: Path, name: str, text: str) -> None:
    path = source.joinpath(*name.split("."))
    if text.startswith("PACKAGE"):
        path.mkdir(parents=True, exist_ok=True)
        path = path / "__init__.py"
        text = text.removeprefix("PACKAGE")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path = path.with_suffix(".py")
    path.write_text(text, encoding="utf-8")


def configs(name: str) -> list[object]:
    return sys.modules[name].calls  # type: ignore[no-any-return]


COMPONENT = "calls = []\n\ndef apply(ctx, config):\n    calls.append(config)\n"


@pytest.mark.parametrize(
    ("entry_name", "package", "module"),
    [
        ("imp_plain", None, "imp_plain"),
        ("imp_attr:plugin", None, "imp_attr"),
        ("imp_path:holder.plugin", None, "imp_path"),
        (".imp_rel", "imp_pkg", "imp_pkg.imp_rel"),
    ],
)
def test_a_name_resolves_to_a_module_an_attribute_or_a_path_of_attributes(
    root: Context, source: Path, entry_name: str, package: str | None, module: str
) -> None:
    write(source, "imp_pkg", "PACKAGE")
    write(source, "imp_pkg.imp_rel", COMPONENT)
    write(source, "imp_plain", COMPONENT)
    function = "calls = []\n\ndef plugin(ctx, config):\n    calls.append(config)\n"
    write(source, "imp_attr", function)
    write(
        source,
        "imp_path",
        function + "\nclass holder:\n    plugin = staticmethod(plugin)\n",
    )
    loader = start(root, package)

    loader.create({"name": entry_name, "config": 7}).result()

    assert configs(module) == [7]


@pytest.mark.divergence("loader-import")
def test_a_missing_entry_module_and_a_missing_module_of_the_component_differ(
    root: Context, source: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(
        source,
        "imp_broken",
        "import imp_missing_dependency\n\ndef apply(ctx, config): ...\n",
    )
    loader = start(root)

    first = loader.create({"id": "gone", "name": "imp_missing_module"}).result()
    second = loader.create({"id": "broken", "name": "imp_broken"}).result()

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "cannot find module 'imp_missing_module'",
        "No module named 'imp_missing_dependency'",
    ]
    assert (first, second) == ("gone", "broken")
    assert loader.resolve("gone").live_fiber() is None
    assert loader.resolve("broken").live_fiber() is None


@pytest.mark.upstream(
    "loader/import.spec.ts::Loader: component import::leave a module imported by another one to Node"  # noqa: E501
)
@pytest.mark.divergence("loader-import")
def test_an_import_error_of_the_component_keeps_its_own_message(
    root: Context, source: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(source, "imp_dep_user", "import imp_dependency_x\n")
    loader = start(root)

    loader.create({"name": "imp_dep_user"}).result()

    [record] = caplog.records
    assert isinstance(record.exc_info[1], ModuleNotFoundError)  # type: ignore[index]
    assert record.exc_info[1].name == "imp_dependency_x"  # type: ignore[index, union-attr]


def test_a_name_that_is_not_a_component_is_logged_and_does_not_start(
    root: Context, source: Path, caplog: pytest.LogCaptureFixture
) -> None:
    write(source, "imp_value", "value = 3\n")
    loader = start(root)

    loader.create({"id": "v", "name": "imp_value:value"}).result()

    assert "is not a component" in caplog.text
    assert loader.resolve("v").live_fiber() is None
