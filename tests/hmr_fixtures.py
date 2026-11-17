"""Plugin packages that a test writes under `tmp_path` and imports from `sys.path`."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest


class PluginDir:
    """A directory on `sys.path` that holds the modules of one test.

    `write` creates or replaces a module file. `forget` removes the modules of the
    directory from `sys.modules` and the directory from `sys.path`, so that a later test
    does not see them.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self, relative: str, source: str = "") -> Path:
        """Write `source` to the file `relative` (such as `pkg/__init__.py`)."""
        file = self.path / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source, encoding="utf-8")
        return file

    def forget(self) -> None:
        """Remove the modules below the directory and the directory itself."""
        for name, module in list(sys.modules.items()):
            origin = module.__dict__.get("__file__")
            if origin is not None and Path(origin).is_relative_to(self.path):
                del sys.modules[name]
        sys.path.remove(str(self.path))


@pytest.fixture
def plugins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[PluginDir]:
    """An empty `PluginDir` on `sys.path`; the test writes no bytecode."""
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    directory = PluginDir(tmp_path.resolve() / "plugins")
    directory.path.mkdir()
    sys.path.insert(0, str(directory.path))
    try:
        yield directory
    finally:
        directory.forget()
