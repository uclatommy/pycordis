import importlib.metadata
import tomllib
from pathlib import Path

import pycordis

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_version_is_the_installed_distribution_version() -> None:
    with PYPROJECT.open("rb") as fh:
        source_version = tomllib.load(fh)["project"]["version"]
    installed_version = importlib.metadata.version("pycordis")
    assert pycordis.__version__ == installed_version == source_version
