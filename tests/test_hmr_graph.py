"""Tests of `source_graph`: the module names that a loaded module imports."""

import importlib
import importlib.util
import json
import sys
import sysconfig
from pathlib import Path
from types import ModuleType

import pytest
from hmr_fixtures import PluginDir

from pycordis.hmr import source_graph

PKG = {"pkg/__init__.py": "x = 1", "pkg/leaf.py": "x = 1"}
SUB = {**PKG, "pkg/sub/__init__.py": "", "pkg/sub/leaf.py": "x = 1"}

# (files, the module to import, its expected edges)
CASES = {
    "import-dotted": ({**PKG, "m.py": "import pkg.leaf"}, "m", {"pkg", "pkg.leaf"}),
    "from-module": ({**PKG, "m.py": "from pkg import leaf"}, "m", {"pkg", "pkg.leaf"}),
    "from-attribute": (
        {**PKG, "m.py": "from pkg import x"},
        "m",
        {"pkg"},
    ),
    "from-dotted": (
        {**SUB, "m.py": "from pkg.sub import leaf"},
        "m",
        {"pkg", "pkg.sub", "pkg.sub.leaf"},
    ),
    "star": ({**PKG, "m.py": "from pkg import *"}, "m", {"pkg"}),
    "function-and-if": (
        {
            "a.py": "",
            "b.py": "",
            "m.py": "if True:\n    import a\n\ndef f():\n    import b\n",
        },
        "m",
        {"a", "b"},
    ),
    "dynamic-import-is-no-edge": (
        {"a.py": "", "m.py": "import importlib\nimportlib.import_module('a')"},
        "m",
        {"importlib"},
    ),
    "relative-sibling": (
        {**PKG, "pkg/m.py": "from . import leaf\nfrom .leaf import x"},
        "pkg.m",
        {"pkg", "pkg.leaf"},
    ),
    "relative-parent": (
        {**SUB, "pkg/sub/m.py": "from .. import leaf\nfrom ..leaf import x"},
        "pkg.sub.m",
        {"pkg", "pkg.leaf", "pkg.sub"},
    ),
    "package-init-and-parent": (
        {"pkg/__init__.py": "from . import leaf", "pkg/leaf.py": ""},
        "pkg",
        {"pkg.leaf"},
    ),
    "submodule-has-parent-edge": ({**PKG, "pkg/sub.py": ""}, "pkg.sub", {"pkg"}),
}


@pytest.mark.parametrize("case", CASES)
def test_graph_lists_the_modules_that_a_file_imports(
    plugins: PluginDir, case: str
) -> None:
    files, name, expected = CASES[case]
    for relative, source in files.items():
        plugins.write(relative, source)
    importlib.invalidate_caches()
    importlib.import_module(name.split(".")[0])
    module = importlib.import_module(name)

    edges = source_graph([str(plugins.path)])(module)

    assert edges is not None
    assert set(edges) == expected
    assert len(edges) == len(set(edges))


@pytest.mark.parametrize("package", ["", "pkg"])
def test_graph_skips_a_relative_import_above_the_top_package(
    plugins: PluginDir, package: str
) -> None:
    file = plugins.write("m.py", "import json\nfrom ... import a")
    module = ModuleType("m")
    module.__package__ = package
    module.__spec__ = importlib.util.spec_from_file_location("m", file)

    assert source_graph([str(plugins.path)])(module) == ("json",)


def test_graph_finds_an_imported_submodule_that_is_not_loaded(
    plugins: PluginDir,
) -> None:
    for relative, source in {**PKG, "m.py": "from pkg import leaf"}.items():
        plugins.write(relative, source)
    module = importlib.import_module("m")
    del sys.modules["pkg.leaf"]

    assert set(source_graph([str(plugins.path)])(module) or ()) == {"pkg", "pkg.leaf"}


def test_graph_raises_syntax_error_for_a_source_that_is_not_python(
    plugins: PluginDir,
) -> None:
    plugins.write("m.py")
    module = importlib.import_module("m")
    plugins.write("m.py", "import (")

    with pytest.raises(SyntaxError):
        source_graph([str(plugins.path)])(module)


def test_graph_reads_the_file_of_a_module_without_an_import_spec(
    plugins: PluginDir,
) -> None:
    script = ModuleType("__main__")
    script.__file__ = str(plugins.write("script.py", "import json"))

    assert script.__spec__ is None
    assert source_graph([str(plugins.path)])(script) == ("json",)


def test_graph_has_no_edges_for_a_module_that_is_not_python_source(
    plugins: PluginDir,
) -> None:
    file = plugins.write("m.so")
    module = ModuleType("m")
    module.__spec__ = importlib.util.spec_from_file_location("m", file)

    assert source_graph([str(plugins.path)])(module) == ()


def test_graph_gives_none_outside_scope_the_installation_and_without_a_file(
    plugins: PluginDir, tmp_path: Path
) -> None:
    plugins.write("m.py")
    module = importlib.import_module("m")
    paths = sysconfig.get_paths()
    # A virtual environment inside the scope: its library directories are excluded.
    scope = [str(Path(paths["purelib"]).parent), str(plugins.path)]

    assert source_graph([str(tmp_path / "other")])(module) is None
    assert source_graph(scope)(module) == ()
    assert source_graph(scope)(pytest) is None  # below `purelib`, inside the scope
    assert source_graph([paths["stdlib"]])(json) is None
    assert source_graph(scope)(sys) is None  # built-in: no file
