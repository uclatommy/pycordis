import importlib.metadata
import inspect
import tomllib
import types
from pathlib import Path

import pytest

import pycordis

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_is_the_installed_distribution_version() -> None:
    with PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    source_version = project["version"]
    installed_version = importlib.metadata.version(project["name"])
    assert pycordis.__version__ == installed_version == source_version


PUBLIC_API = {
    "Component",
    "Context",
    "CordisError",
    "Deferred",
    "Disposer",
    "EffectHandle",
    "Fiber",
    "FiberState",
    "FramePumpScheduler",
    "ImmediateScheduler",
    "Issue",
    "Key",
    "Keyable",
    "Scheduler",
    "Service",
    "ValidationError",
    "__version__",
    "inject",
}


def test_the_public_api_is_the_set_that_the_package_exports() -> None:
    namespace: dict[str, object] = {}
    exec("from pycordis import *", namespace)  # noqa: S102

    assert set(pycordis.__all__) == PUBLIC_API
    assert PUBLIC_API <= namespace.keys()


@pytest.mark.parametrize("name", sorted(PUBLIC_API - {"__version__"}))
def test_every_exported_class_and_function_has_a_docstring(name: str) -> None:
    exported = getattr(pycordis, name)
    if isinstance(exported, (type, types.FunctionType)):
        assert inspect.getdoc(exported)
