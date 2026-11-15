import importlib.metadata

import pycordis


def test_version_is_the_installed_distribution_version() -> None:
    assert pycordis.__version__ == importlib.metadata.version("pycordis")
