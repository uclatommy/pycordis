"""`Hmr.reconcile`: reload the changed modules and move component fibers."""

import importlib.machinery
import importlib.util
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from hmr_fixtures import PluginDir

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError
from pycordis.fiber import Fiber, FiberState
from pycordis.hmr import Hmr, HmrConfig, ReloadResult, Skip, SkipReason


def component(name: str, value: str, body: str = "") -> str:
    """Return the source of a plugin module that answers `reconcile/<name>`."""
    return textwrap.dedent(f"""
        from hmr_fixtures import STATS

        def apply(ctx, config=None):
            ctx.on("reconcile/{name}", lambda: "{value}")
        """) + textwrap.indent(textwrap.dedent(body), "    ")


class Env:
    """A root context with `Hmr` over a plugin directory."""

    def __init__(self, ctx: Context, plugins: PluginDir, config: HmrConfig) -> None:
        self.ctx = ctx
        self.plugins = plugins
        self.fiber = ctx.use(Hmr, config)
        self.hmr: Hmr = ctx.get("hmr")

    def path(self, name: str) -> Path:
        return self.plugins.path / f"{name}.py"

    def write(self, name: str, source: str) -> Path:
        return self.plugins.write(f"{name}.py", source)

    def load(self, name: str, source: str, config: Any = None) -> Fiber:
        """Write the module `name` and run its `apply` in a fiber."""
        self.write(name, source)
        module = __import__(name)
        return self.ctx.use(module.apply, config)

    def reload(self, *paths: Any) -> ReloadResult:
        return self.hmr.reconcile(lambda: list(paths)).result()

    def answer(self, name: str) -> Any:
        return self.ctx.bail(f"reconcile/{name}")


type MakeEnv = Callable[..., Env]


@pytest.fixture
def make_env(
    root: Context, plugins: PluginDir, request: pytest.FixtureRequest
) -> MakeEnv:
    """Start `Hmr` over the plugin directory; the fixture ends it with the test.

    One `Hmr` is active in a process, so a test that ends without disposing it would
    block the `Hmr` of the next test.
    """

    def make(**config: Any) -> Env:
        environment = Env(
            root, plugins, HmrConfig(scope=(str(plugins.path),), **config)
        )
        request.addfinalizer(environment.fiber.dispose)
        return environment

    return make


@pytest.fixture
def env(make_env: MakeEnv) -> Env:
    return make_env()


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::replaces a component that no entry manages"
)
def test_a_changed_module_moves_the_fiber_of_its_component_to_the_new_code(
    env: Env, stats: dict[str, Any]
) -> None:
    body = "ctx.effect(lambda: lambda: STATS.update(disposed=True))"
    fiber = env.load("direct", component("direct", "v1", body), {"size": 1})
    assert env.answer("direct") == "v1"

    path = env.write("direct", component("direct", "v2"))
    result = env.reload(path)

    assert result.settled == [fiber]
    assert (result.aborted, result.skipped) == ([], [])
    assert (fiber.state, stats.get("disposed")) == (FiberState.ACTIVE, True)
    assert env.answer("direct") == "v2"


@pytest.mark.upstream("hmr/reconcile.spec.ts::Hmr.reconcile::replaces a service")
def test_a_changed_module_replaces_the_service_that_its_class_provides(
    env: Env,
) -> None:
    def source(value: str) -> str:
        return textwrap.dedent(f"""
            from pycordis.service import Service

            class ReconcileService(Service):
                provide = "reconcileService"
                value = "{value}"

                def __init__(self, ctx, config=None):
                    super().__init__(ctx)
            """)

    env.write("service", source("v1"))
    module = __import__("service")
    env.ctx.use(module.ReconcileService)
    assert env.ctx.get("reconcileService").value == "v1"

    env.reload(env.write("service", source("v2")))

    assert env.ctx.get("reconcileService").value == "v2"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::replaces a component when one of its "
    "dependencies changes"
)
def test_a_component_whose_dependency_changed_moves_and_an_unrelated_one_stays(
    env: Env, stats: dict[str, Any]
) -> None:
    dep = env.write("dep", "value = 'v1'\n")
    env.write("common", "value = 'common'\n")
    env.load(
        "dependent",
        """
        import common
        from dep import value

        def apply(ctx, config=None):
            ctx.on("reconcile/dependent", lambda: value)
        """.replace("\n        ", "\n"),
    )
    env.load(
        "sibling",
        "import common\nfrom hmr_fixtures import STATS\n\n"
        "def apply(ctx, config=None):\n"
        "    STATS['applied'] = STATS.get('applied', 0) + 1\n",
    )
    assert stats["applied"] == 1

    env.write("dep", "value = 'v2'\n")
    result = env.reload(dep)

    assert len(result.settled) == 1
    assert env.answer("dependent") == "v2"
    assert stats["applied"] == 1


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::replaces a component when a module inside "
    "a package changes"
)
def test_a_change_inside_a_package_reaches_the_component_that_imports_the_package(
    env: Env,
) -> None:
    env.plugins.write("shared/__init__.py", "from .lib import value\n")
    lib = env.plugins.write("shared/lib.py", "value = 'v1'\n")
    env.load(
        "package_dependent",
        "from shared import value\n\n"
        "def apply(ctx, config=None):\n"
        "    ctx.on('reconcile/package', lambda: value)\n",
    )
    assert env.answer("package") == "v1"

    env.plugins.write("shared/lib.py", "value = 'v2'\n")
    result = env.reload(lib)

    assert len(result.settled) == 1
    assert env.answer("package") == "v2"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::replaces the components below a "
    "directory root"
)
def test_a_directory_root_matches_whole_path_segments(env: Env) -> None:
    def dependent(name: str, package: str) -> str:
        return (
            f"from {package}.dep import value\n\n"
            "def apply(ctx, config=None):\n"
            f"    ctx.on('reconcile/{name}', lambda: value)\n"
        )

    for package in ("root", "root_sibling"):
        env.plugins.write(f"{package}/__init__.py", "")
        env.plugins.write(f"{package}/dep.py", "value = 'v1'\n")
    env.load("root_dependent", dependent("root", "root"))
    env.load("sibling_dependent", dependent("sibling", "root_sibling"))

    for package in ("root", "root_sibling"):
        env.plugins.write(f"{package}/dep.py", "value = 'v2'\n")
    result = env.reload(str(env.plugins.path / "root") + "/")

    assert len(result.settled) == 1
    assert (env.answer("root"), env.answer("sibling")) == ("v2", "v1")


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::rejects a relative path as a root"
)
@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::rejects a file URL as a root"
)
@pytest.mark.parametrize(
    ("bad_root", "shown"),
    [
        ("./relative.py", '"./relative.py"'),
        ("file:///tmp/relative.py", '"file:///tmp/relative.py"'),
        (7, "7"),
    ],
)
def test_a_root_that_is_not_an_absolute_path_rejects_and_keeps_the_modules(
    env: Env, bad_root: Any, shown: str
) -> None:
    path = env.write("relative", component("relative", "v1"))
    env.load("relative", component("relative", "v1"))
    env.write("relative", component("relative", "v2"))
    module = sys.modules["relative"]

    result = env.hmr.reconcile(lambda: [path, bad_root])

    with pytest.raises(
        TypeError, match=f"^invalid root {shown}, expected an absolute path$"
    ):
        result.result()
    assert sys.modules["relative"] is module
    assert env.answer("relative") == "v1"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::rejects with the error of the callback"
)
@pytest.mark.parametrize("deferred", [False, True])
def test_an_error_of_the_changes_callback_rejects_the_transaction(
    env: Env, deferred: bool
) -> None:
    env.load("callback", component("callback", "v1"))
    env.write("callback", component("callback", "v2"))
    module = sys.modules["callback"]
    failure = RuntimeError("callback failed")

    def changes() -> Any:
        if not deferred:
            raise failure
        late = Deferred[Any]()
        late.reject(failure)
        return late

    with pytest.raises(RuntimeError, match="callback failed"):
        env.hmr.reconcile(changes).result()
    assert sys.modules["callback"] is module
    assert env.answer("callback") == "v1"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::refreshes a module that no component "
    "depends on for its next import"
)
def test_a_changed_module_that_no_component_uses_is_imported_new_on_demand(
    env: Env,
) -> None:
    orphan = env.write("orphan", "value = 1\n")
    before = __import__("orphan")
    env.write("orphan", "value = 2\n")
    data = env.plugins.write("data.txt", "data")

    result = env.reload(orphan, data)

    assert result.settled == []
    assert before.value == 1  # a reference to the old module keeps the old code
    assert "orphan" not in sys.modules
    assert __import__("orphan").value == 2


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::does not apply a unit whose module fails "
    "to import"
)
@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::does not apply a unit whose module does "
    "not export a component"
)
@pytest.mark.parametrize(
    ("broken", "message"),
    [
        ("def apply(ctx {{{ BROKEN\n", SyntaxError),
        ("apply = 'not a function'\n", "invalid component at abort"),
    ],
)
def test_a_unit_that_fails_to_import_or_exports_no_component_is_aborted(
    env: Env, stats: dict[str, Any], broken: str, message: Any
) -> None:
    body = "ctx.effect(lambda: lambda: STATS.update(disposed=True))"
    fiber = env.load("abort", component("abort", "v1", body))
    module = sys.modules["abort"]

    path = env.write("abort", broken)
    result = env.reload(path)

    assert result.settled == []
    assert [abort.modules for abort in result.aborted] == [["abort"]]
    error = result.aborted[0].error
    if isinstance(message, str):
        assert str(error).startswith(message)
    else:
        assert isinstance(error, message)
    assert sys.modules["abort"] is module
    assert (fiber.state, stats.get("disposed"), env.answer("abort")) == (
        FiberState.ACTIVE,
        None,
        "v1",
    )

    # the restored module is current, so the next change applies
    result = env.reload(env.write("abort", component("abort", "v2")))
    assert (len(result.settled), result.aborted) == (1, [])
    assert env.answer("abort") == "v2"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::imports a module again after it failed in "
    "an aborted attempt"
)
def test_a_module_that_failed_in_an_aborted_unit_imports_again_with_the_entry(
    env: Env,
) -> None:
    env.load("added_entry", component("added", "v1"))
    entry = env.write("added_entry", "import added_dep\n" + component("added", "v2"))
    dep = env.write("added_dep", "raise RuntimeError('added v1')\n")
    assert len(env.reload(entry).aborted) == 1

    # only the entry is reconciled again, as the caller knows no more
    dep.write_text("", encoding="utf-8")
    result = env.reload(entry)

    assert result.aborted == []
    assert env.answer("added") == "v2"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::leaves a fiber failed when the new "
    "component fails to apply"
)
def test_a_new_component_that_fails_to_apply_leaves_its_fiber_failed(
    env: Env,
) -> None:
    env.load("failed", component("failed", "v1"))

    path = env.write(
        "failed", component("failed", "v2", "raise RuntimeError('apply failed')")
    )
    result = env.reload(path)

    assert len(result.settled) == 1
    assert result.settled[0].state == FiberState.FAILED
    assert str(result.settled[0].error) == "apply failed"

    # the next change retries it
    retried = env.reload(env.write("failed", component("failed", "v3")))
    assert len(retried.settled) == 1
    assert env.answer("failed") == "v3"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::keeps the metadata of an entry that "
    "re-exports the changed component"
)
def test_an_entry_module_that_re_exports_a_component_keeps_its_metadata(
    env: Env,
) -> None:
    env.ctx.provide("reconcileGate", {})

    def impl(value: str) -> str:
        return (
            "def apply(ctx, config=None):\n"
            f"    ctx.on('reconcile/reexport', lambda: ('{value}', config))\n"
        )

    implementation = env.write("reexport_impl", impl("v1"))
    env.write(
        "reexport_entry",
        "from reexport_impl import apply\n"
        "inject = ['reconcileGate']\n"
        "def Config(value):\n"
        "    return {**value, 'validated': True}\n",
    )
    original = __import__("reexport_entry")
    env.ctx.use(original, {})

    env.write("reexport_impl", impl("v2"))
    result = env.reload(implementation)

    assert len(result.settled) == 1
    assert list(result.settled[0].inject) == ["reconcileGate"]
    assert env.answer("reexport") == ("v2", {"validated": True})
    # the entry module is imported new as well, and it stands for the same fiber
    refreshed = __import__("reexport_entry")
    assert refreshed is not original
    assert env.ctx.registry.get(refreshed) is result.settled[0].runtime


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::runs transactions one at a time"
)
def test_a_transaction_waits_for_the_running_one(env: Env) -> None:
    slow = env.load("queue_slow", component("slow", "v1"))
    fast = env.load("queue_fast", component("fast", "v1"))
    env.write("queue_slow", component("slow", "v2"))
    env.write("queue_fast", component("fast", "v2"))
    gate = Deferred[list[str]]()
    order: list[str] = []

    def second() -> list[str]:
        order.append("second")
        return [str(env.plugins.path / "queue_fast.py")]

    first = env.hmr.reconcile(lambda: gate)
    queued = env.hmr.reconcile(second)

    assert (first.done(), queued.done(), order) == (False, False, [])
    gate.resolve([str(env.plugins.path / "queue_slow.py")])

    assert first.result().settled == [slow]
    assert queued.result().settled == [fast]
    assert order == ["second"]


def test_the_old_graph_of_a_module_decides_which_modules_reload(env: Env) -> None:
    # `mid` no longer imports `leaf` on disk, but the transaction knows its old edges
    leaf = env.write("leaf", "value = 'v1'\n")
    env.write("mid", "import leaf\nvalue = leaf.value\n")
    env.load(
        "top",
        "import mid\n\n"
        "def apply(ctx, config=None):\n"
        "    ctx.on('reconcile/top', lambda: mid.value)\n",
    )
    env.reload()  # the service sees the modules and their imports
    env.write("mid", "value = 'standalone'\n")
    env.write("leaf", "value = 'v2'\n")

    result = env.reload(leaf)

    assert len(result.settled) == 1
    assert env.answer("top") == "standalone"


def test_a_component_that_an_importer_takes_from_an_unchanged_module_keeps_its_fibers(
    env: Env, stats: dict[str, Any]
) -> None:
    env.write("dep", "value = 1\n")
    env.write(
        "base",
        "from hmr_fixtures import STATS\n\n"
        "def apply(ctx, config=None):\n"
        "    STATS['applied'] = STATS.get('applied', 0) + 1\n",
    )
    env.load("wrapper", "import dep\nfrom base import apply\n")
    wrapper = sys.modules["wrapper"]

    result = env.reload(env.write("dep", "value = 2\n"))

    assert result.settled == []
    assert sys.modules["wrapper"] is not wrapper
    assert stats["applied"] == 1


def test_an_aborted_unit_restores_the_importer_that_reaches_it_and_not_an_orphan(
    env: Env,
) -> None:
    # `ui` imports the component, so it reloads with it, but no component exports it
    env.load("comp", component("comp", "v1"))
    env.write("ui", "def use():\n    import comp\n")
    env.write("orphan", "value = 1\n")
    __import__("ui")
    __import__("orphan")
    env.reload()
    ui = sys.modules["ui"]

    broken = env.write("comp", "def apply(ctx {{{ BROKEN\n")
    result = env.reload(broken, env.plugins.path / "orphan.py")

    assert [abort.modules for abort in result.aborted] == [["comp", "ui"]]
    assert sys.modules["ui"] is ui
    assert "orphan" not in sys.modules


def test_a_package_submodule_that_reloads_is_bound_on_its_new_package(
    env: Env,
) -> None:
    env.plugins.write("pkg/__init__.py", "")
    env.plugins.write("pkg/leaf.py", "value = 'v1'\n")
    env.load(
        "pkg_user",
        "import pkg.leaf\n\n"
        "def apply(ctx, config=None):\n"
        "    ctx.on('reconcile/pkg', lambda: pkg.leaf.value)\n",
    )
    old_leaf = sys.modules["pkg.leaf"]

    env.reload(env.plugins.write("pkg/leaf.py", "value = 'v2'\n"))

    assert sys.modules["pkg"].leaf is sys.modules["pkg.leaf"] is not old_leaf
    assert env.answer("pkg") == "v2"

    broken = env.plugins.write("pkg/leaf.py", "value = (\n")
    new_leaf = sys.modules["pkg.leaf"]
    assert len(env.reload(broken).aborted) == 1
    assert sys.modules["pkg"].leaf is sys.modules["pkg.leaf"] is new_leaf


def test_a_failed_replace_is_logged_and_does_not_reject_the_transaction(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    env.load("replace_fails", component("replace_fails", "v1"))

    def refuse(old: Any, new: Any) -> list[Fiber]:
        raise RuntimeError("replace refused")

    monkeypatch.setattr(env.ctx.registry, "replace", refuse)
    path = env.write("replace_fails", component("replace_fails", "v2"))
    with caplog.at_level("WARNING"):
        result = env.reload(path)

    assert (result.settled, result.aborted) == ([], [])
    assert "failed to reload component at replace_fails" in caplog.text


def test_a_value_that_cannot_be_hashed_is_not_a_component(env: Env) -> None:
    source = (
        component("unhashable", "v1") + "\nclass Unhashable:\n"
        "    __hash__ = None\n"
        "    def __call__(self):\n"
        "        pass\n\n"
        "thing = Unhashable()\n"
    )
    env.load("unhashable", source)

    result = env.reload(env.write("unhashable", source.replace("v1", "v2")))

    assert len(result.settled) == 1
    assert env.answer("unhashable") == "v2"


def test_a_host_graph_decides_which_modules_reload(
    root: Context,
    plugins: PluginDir,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    # `virtual` has no file, so no changed path matches it
    monkeypatch.setitem(sys.modules, "virtual", ModuleType("virtual"))
    path = plugins.write("listed.py", "value = 1\n")
    plugins.write("unlisted.py", "value = 1\n")
    __import__("listed")
    unlisted = __import__("unlisted")

    def graph(module: ModuleType) -> list[str] | None:
        return [] if module.__name__ in {"virtual", "listed"} else None

    class HostHmr(Hmr):
        def __init__(self, ctx: Context, config: HmrConfig | None = None) -> None:
            super().__init__(ctx, config, graph=graph)

    request.addfinalizer(root.use(HostHmr).dispose)
    hmr: Hmr = root.get("hmr")
    unlisted_path = plugins.write("unlisted.py", "value = 2\n")
    plugins.write("listed.py", "value = 2\n")

    result = hmr.reconcile(lambda: [path, unlisted_path]).result()

    assert result == ReloadResult()
    assert "listed" not in sys.modules  # evicted: its next import is new
    assert sys.modules["unlisted"] is unlisted  # outside the graph: never reloads
    assert "virtual" in sys.modules


@pytest.mark.parametrize("failure", ["graph", "interrupt"])
def test_an_error_that_ends_the_transaction_leaves_the_modules_as_they_were(
    root: Context,
    plugins: PluginDir,
    request: pytest.FixtureRequest,
    failure: str,
) -> None:
    plugins.write("rb_helper.py", "value = 1\n")
    entry = plugins.write(
        "rb_entry.py",
        component("rb", "v1", "").replace(
            "from hmr_fixtures import STATS",
            "import rb_helper\nfrom hmr_fixtures import STATS",
        ),
    )
    module = __import__("rb_entry")
    helper = sys.modules["rb_helper"]
    fiber = root.use(module.apply)
    old = set(sys.modules.values())

    def graph(candidate: ModuleType) -> list[str] | None:
        if failure == "graph" and candidate not in old:
            raise ValueError("host graph failed")
        return {"rb_entry": ["rb_helper"], "rb_helper": []}.get(candidate.__name__)

    class HostHmr(Hmr):
        def __init__(self, ctx: Context, config: HmrConfig | None = None) -> None:
            super().__init__(ctx, config, graph=graph)

    config = HmrConfig(scope=(str(plugins.path),))
    request.addfinalizer(root.use(HostHmr, config).dispose)
    hmr: Hmr = root.get("hmr")
    before = dict(sys.modules)
    source = "import rb_helper\nvalue = 2\n"
    if failure == "interrupt":
        source = "raise KeyboardInterrupt\n"
    changed = plugins.write("rb_helper.py", source)

    with pytest.raises(ValueError if failure == "graph" else KeyboardInterrupt):
        hmr.reconcile(lambda: [changed, entry]).result()

    assert sys.modules == before
    assert sys.modules["rb_entry"] is module
    assert sys.modules["rb_helper"] is helper
    assert fiber.state is FiberState.ACTIVE
    assert root.bail("reconcile/rb") == "v1"


def test_every_entry_module_of_a_component_imports_again_with_the_implementation(
    env: Env,
) -> None:
    def impl(value: str) -> str:
        return f"def apply(ctx, config=None):\n    ctx.on('r/e', lambda: '{value}')\n"

    implementation = env.write("entry_impl", impl("v1"))
    env.write("entry_a", "from entry_impl import apply\n")
    env.write("entry_b", "from entry_impl import apply\n")
    env.ctx.use(__import__("entry_a"))
    other = __import__("entry_b")
    env.write("entry_impl", impl("v2"))

    result = env.reload(implementation)

    assert len(result.settled) == 1
    assert sys.modules["entry_b"] is not other
    assert env.ctx.bail("r/e") == "v2"


def test_a_file_that_cannot_be_parsed_is_logged_and_has_no_imports(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    env.load("parse_ok", component("parse_ok", "v1"))
    broken = env.write("parse_broken", "value = (\n")
    sys.modules["parse_broken"] = ModuleType("parse_broken")
    sys.modules["parse_broken"].__file__ = str(broken)

    with caplog.at_level("WARNING"):
        result = env.reload(env.write("parse_ok", component("parse_ok", "v2")))

    assert len(result.settled) == 1
    assert "cannot read the imports of parse_broken" in caplog.text


@pytest.mark.parametrize("name", ["stray", "__main__"])
def test_a_sys_modules_value_that_is_not_a_module_is_outside_the_graph(
    make_env: MakeEnv, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    # Python 3.12 holds classes as `typing.io` and `typing.re`. An `object()` also has
    # no `__dict__`, so it reaches every reader of a `sys.modules` value.
    stray = object()
    monkeypatch.setitem(sys.modules, name, stray)
    env = make_env()
    importer = component(
        "stray_importer", "v1", f"def unused():\n    from {name} import value\n"
    )
    env.load("stray_importer", importer)

    path = env.write("stray_importer", importer.replace('"v1"', '"v2"'))
    result = env.reload(path)

    assert len(result.settled) == 1
    assert env.answer("stray_importer") == "v2"
    assert sys.modules[name] is stray


def test_paths_that_match_no_loaded_module_change_nothing(env: Env) -> None:
    env.load("untouched", component("untouched", "v1"))
    module = sys.modules["untouched"]
    stray = env.plugins.write("stray.txt", "data")

    results = [env.reload(), env.reload(stray), env.reload(env.plugins.path / "none")]

    assert results == [ReloadResult()] * 3
    assert sys.modules["untouched"] is module


def test_the_default_scope_is_the_working_directory_at_start(
    root: Context,
    plugins: PluginDir,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    monkeypatch.chdir(plugins.path)
    request.addfinalizer(root.use(Hmr).dispose)
    path = plugins.write("cwd_plugin.py", component("cwd", "v1"))
    root.use(__import__("cwd_plugin").apply)
    plugins.write("cwd_plugin.py", component("cwd", "v2"))

    result = root.get("hmr").reconcile(lambda: [path]).result()

    assert len(result.settled) == 1
    assert root.bail("reconcile/cwd") == "v2"


def sharing(name: str, extra: str = "") -> str:
    """Return a component module that answers the value of the module `shared`."""
    return (
        f"import shared\n{extra}\n"
        "def apply(ctx, config=None):\n"
        f"    ctx.on('reconcile/{name}', lambda: shared.value)\n"
    )


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::aborts components that share a changed module together"  # noqa: E501
)
def test_components_that_share_a_changed_module_abort_together(env: Env) -> None:
    env.write("shared", "value = 'v1'\n")
    env.load("left", sharing("left"))
    env.load("right", sharing("right"))

    shared = env.write("shared", "value = 'v2'\n")
    left = env.write("left", sharing("left", "raise RuntimeError('left failed')"))
    result = env.reload(shared, left)

    assert result.settled == []
    assert [abort.modules for abort in result.aborted] == [["left", "right", "shared"]]
    assert (env.answer("left"), env.answer("right")) == ("v1", "v1")


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::applies a unit when another unit fails to import"  # noqa: E501
)
def test_a_unit_applies_when_a_unit_that_shares_only_an_unchanged_module_fails(
    env: Env,
) -> None:
    env.write("shared", "value = 'common'\n")
    env.load("broken", sharing("broken"))
    env.load("healthy", sharing("healthy"))

    broken = env.write("broken", "def apply(ctx {{{ BROKEN\n")
    healthy = env.write("healthy", sharing("healthy", "# v2"))
    result = env.reload(broken, healthy)

    assert len(result.settled) == 1
    assert [abort.modules for abort in result.aborted] == [["broken"]]
    assert env.answer("healthy") == "common"
    assert env.answer("broken") == "common"

    # the failed unit is retried on its own
    env.write("broken", sharing("broken", "# v2"))
    retried = env.reload(broken)
    assert (len(retried.settled), retried.aborted) == (1, [])


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::aborts a component that newly imports a changed module of a failed unit"  # noqa: E501
)
def test_a_component_that_newly_imports_a_module_of_a_failed_unit_joins_its_abort(
    env: Env,
) -> None:
    env.write("shared", "value = 'v1'\n")
    env.load("owner", sharing("owner"))
    env.load("adopter", component("adopter", "v1"))
    env.reload()  # record the imports as they are before the change

    shared = env.write("shared", "value = 'v2'\n")
    owner = env.write("owner", "import shared\nraise RuntimeError('owner failed')\n")
    adopter = env.write("adopter", sharing("adopter"))
    result = env.reload(shared, owner, adopter)

    assert result.settled == []
    assert [abort.modules for abort in result.aborted] == [
        ["adopter", "owner", "shared"]
    ]
    assert (env.answer("owner"), env.answer("adopter")) == ("v1", "v1")


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::shares a module first loaded by a failed unit with a unit that imports it dynamically"  # noqa: E501
)
def test_a_module_that_a_failed_unit_loaded_first_stays_loaded_for_another_unit(
    env: Env,
) -> None:
    env.load("failing", component("failing", "v1"))
    env.load("importer", component("importer", "v1"))
    env.write("late", "state = {}\n")

    failing = env.write(
        "failing", "import late\nraise RuntimeError('failing failed')\n"
    )
    importer = env.write(
        "importer",
        "import importlib\n"
        "state = importlib.import_module('late').state\n\n"
        "def apply(ctx, config=None):\n"
        "    ctx.on('reconcile/importer', lambda: state)\n",
    )
    result = env.reload(failing, importer)

    assert (len(result.settled), len(result.aborted)) == (1, 1)
    assert env.answer("importer") is sys.modules["late"].state


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::restores a changed module outside every component with the failed unit it imports"  # noqa: E501
)
def test_a_module_outside_every_unit_comes_back_with_the_failed_unit_that_it_imports(
    env: Env,
) -> None:
    env.write("shared", "value = 'v1'\n")
    env.write("outside", "import shared\nderived = shared.value\n")
    env.load("owner", sharing("owner"))
    outside = __import__("outside")
    env.reload()

    # the new module needs an attribute that only the new `shared` has
    shared = env.write("shared", "value = 'v2'\nextra = '!'\n")
    changed = env.write(
        "outside", "import shared\nderived = shared.value + shared.extra\n"
    )
    owner = env.write("owner", "import shared\nraise RuntimeError('owner failed')\n")
    result = env.reload(shared, changed, owner)

    assert [abort.modules for abort in result.aborted] == [
        ["outside", "owner", "shared"]
    ]
    assert sys.modules["outside"] is outside
    assert outside.derived == "v1"


def test_a_module_that_reaches_a_committed_unit_stays_evicted_with_a_failed_unit(
    env: Env,
) -> None:
    for name in ("dep_a", "dep_b"):
        env.write(name, "value = 'v1'\n")
    env.write("owner_a", "import dep_a\n" + component("a", "v1"))
    env.write("owner_b", "import dep_b\n" + component("b", "v1"))
    env.write("both", "import dep_a\nimport dep_b\n")
    for name in ("owner_a", "owner_b", "both"):
        __import__(name)
    env.ctx.use(sys.modules["owner_a"].apply)
    env.ctx.use(sys.modules["owner_b"].apply)
    env.reload()
    old_a = sys.modules["dep_a"]

    paths = [
        env.write("dep_a", "value = 'v2'\n"),
        env.write("dep_b", "value = 'v2'\n"),
        env.write("owner_a", "import dep_a\nraise RuntimeError('a failed')\n"),
    ]
    result = env.reload(*paths)

    assert [abort.modules for abort in result.aborted] == [["dep_a", "owner_a"]]
    assert len(result.settled) == 1
    assert (sys.modules["dep_a"], "both" in sys.modules) == (old_a, False)
    assert (env.answer("a"), env.answer("b")) == ("v1", "v1")


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::skips a component whose exporting modules now export different components"  # noqa: E501
)
def test_a_component_whose_exporting_modules_export_different_values_is_skipped(
    env: Env,
) -> None:
    def impl(value: str) -> str:
        return (
            "def apply(ctx, config=None):\n"
            f"    ctx.on('reconcile/split', lambda: '{value}')\n"
        )

    implementation = env.write("split_impl", impl("v1"))
    env.write("split_a", "from split_impl import apply\n")
    env.write("split_b", "from split_impl import apply\n")
    env.ctx.use(__import__("split_a").apply)
    other = __import__("split_b")
    assert env.answer("split") == "v1"

    result = env.reload(implementation, env.write("split_b", impl("other")))

    assert result.settled == []
    assert result.skipped == [
        Skip(["split_a", "split_b", "split_impl"], SkipReason.AMBIGUOUS_EXPORT)
    ]
    assert sys.modules["split_b"] is other
    assert env.answer("split") == "v1"


@pytest.mark.parametrize("source", ["config", "main"])
@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::skips a changed host module below a root and applies the other changes"  # noqa: E501
)
@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::collects the host modules of a main entry started through a link"  # noqa: E501
)
def test_a_changed_host_module_is_skipped_and_the_other_changes_apply(
    source: str,
    make_env: MakeEnv,
    plugins: PluginDir,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugins.write("host.py", component("host", "v1"))
    host = __import__("host")
    if source == "main":
        # a script and the modules that it imports belong to the host
        file = plugins.write("script.py", "import host\n")
        spec = importlib.util.spec_from_file_location("__main__", file)
        assert spec is not None and spec.loader is not None
        script = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, "__main__", script)
        spec.loader.exec_module(script)
    env = make_env(
        host_modules=frozenset({"host"}) if source == "config" else frozenset()
    )
    env.ctx.use(host.apply)
    env.load("other", component("other", "v1"))

    result = env.reload(
        env.write("host", component("host", "v2")),
        env.write("other", component("other", "v2")),
    )

    assert "host" in env.hmr.host_modules
    assert result.skipped == [Skip(["host"], SkipReason.HOST_MODULE)]
    assert sys.modules["host"] is host
    assert (env.answer("host"), env.answer("other")) == ("v1", "v2")


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::skips a changed native addon"
)
def test_a_changed_extension_module_is_skipped(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = env.plugins.write(f"native{importlib.machinery.EXTENSION_SUFFIXES[0]}")
    native = ModuleType("native")
    native.__file__ = str(file)
    native.value = "v1"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "native", native)
    env.load(
        "extension_user",
        "import native\n\n"
        "def apply(ctx, config=None):\n"
        "    ctx.on('reconcile/extension', lambda: native.value)\n",
    )
    env.reload()

    result = env.reload(file)

    assert result == ReloadResult(
        skipped=[Skip(["native"], SkipReason.EXTENSION_MODULE)]
    )
    assert env.answer("extension") == "v1"


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::starts one instance at a time in a thread"
)
@pytest.mark.slow
def test_one_hmr_is_active_in_a_process(env: Env) -> None:
    scheduler = env.ctx.scheduler

    second = Context(scheduler).use(Hmr)

    assert second.state == FiberState.FAILED
    assert str(second.error) == "another HMR instance is active in this process"
    assert env.ctx.get("hmr") is not None

    env.fiber.dispose()
    third = Context(scheduler).use(Hmr)
    try:
        assert third.state == FiberState.ACTIVE
    finally:
        third.dispose()


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::runs the callback inside the transaction"
)
def test_a_call_from_the_changes_callback_rejects_and_a_later_call_waits(
    env: Env,
) -> None:
    gate = Deferred[list[str]]()
    order: list[str] = []
    nested: list[Deferred[ReloadResult]] = []

    def first_changes() -> Deferred[list[str]]:
        nested.append(env.hmr.reconcile(lambda: []))
        return gate

    def second_changes() -> list[str]:
        order.append("second")
        return []

    first = env.hmr.reconcile(first_changes)
    second = env.hmr.reconcile(second_changes)

    with pytest.raises(
        CordisError, match="cannot start a transaction inside another transaction"
    ):
        nested[0].result()
    assert (first.done(), second.done(), order) == (False, False, [])
    gate.resolve([])
    first.result()
    second.result()
    assert order == ["second"]


NESTED = """
from hmr_fixtures import STATS

{keyword}def apply(ctx, config=None):
    {gate}
    STATS['nested'] = ctx.get('hmr').reconcile(lambda: [])
    STATS['ctx'] = ctx
"""


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "after-a-wait"])
@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::rejects a transaction started inside another one"  # noqa: E501
)
def test_a_moved_fiber_cannot_start_a_transaction_until_the_transaction_ends(
    env: Env, stats: dict[str, Any], asynchronous: bool
) -> None:
    source = NESTED.format(
        keyword="async " if asynchronous else "",
        gate="await STATS['gate']" if asynchronous else "pass",
    )
    stats["gate"] = Deferred.resolved(None)
    env.load("nested", source)
    assert stats["nested"].result() == ReloadResult()  # no transaction runs yet

    gate = stats["gate"] = Deferred[None]()
    transaction = env.hmr.reconcile(lambda: [env.write("nested", source + "# v2")])
    assert transaction.done() is not asynchronous
    gate.resolve(None)

    assert len(transaction.result().settled) == 1
    with pytest.raises(
        CordisError, match="cannot start a transaction inside another transaction"
    ):
        stats["nested"].result()
    # the transaction is over, so the same call from the same fiber runs
    later = stats["ctx"].get("hmr").reconcile(lambda: [])
    assert later.result() == ReloadResult()


TREE = """
from hmr_fixtures import STATS

async def child(ctx, config=None):
    await STATS['gate']
    STATS['nested'] = ctx.get('hmr').reconcile(lambda: [])

async def apply(ctx, config=None):
    ctx.use(child)
    await STATS['hold']
"""


@pytest.mark.upstream(
    "hmr/reconcile.spec.ts::Hmr.reconcile::rejects a transaction started inside another one"  # noqa: E501
)
def test_a_fiber_below_a_moved_fiber_cannot_start_a_transaction(
    env: Env, stats: dict[str, Any]
) -> None:
    stats["gate"] = stats["hold"] = Deferred.resolved(None)
    env.load("tree", TREE)
    gate, hold = stats["gate"], stats["hold"] = Deferred[None](), Deferred[None]()

    transaction = env.hmr.reconcile(lambda: [env.write("tree", TREE + "# v2")])
    gate.resolve(None)

    assert not transaction.done()
    with pytest.raises(
        CordisError, match="cannot start a transaction inside another transaction"
    ):
        stats["nested"].result()
    hold.resolve(None)
    assert len(transaction.result().settled) == 1


def test_a_main_module_without_a_file_adds_no_host_modules(
    make_env: MakeEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "__main__", ModuleType("__main__"))

    env = make_env()

    assert env.hmr.host_modules == frozenset()


def test_a_module_that_a_kept_unit_loaded_against_a_restored_module_is_evicted(
    env: Env,
) -> None:
    env.write("shared", "value = 'v1'\n")
    env.load("owner", sharing("owner"))
    env.load("adopter", component("adopter", "v1"))
    env.reload()

    shared = env.write("shared", "value = 'v2'\n")
    env.write("late", "import shared\nvalue = shared.value\n")
    owner = env.write("owner", "import shared\nraise RuntimeError('owner failed')\n")
    adopter = env.write("adopter", "import late\n" + component("adopter", "v2"))
    result = env.reload(shared, owner, adopter)

    assert [abort.modules for abort in result.aborted] == [
        ["adopter", "owner", "shared"]
    ]
    # `late` ran against the new `shared`, which did not stay
    assert "late" not in sys.modules


def test_the_new_imports_of_a_failed_module_join_it_to_the_unit_that_it_reaches(
    env: Env,
) -> None:
    # `broken` did not import `shared` before. Its new source imports it and then
    # fails, so only the new file tells that `broken` and `user` share a unit.
    env.write("shared", "value = 'v1'\n")
    env.load("user", sharing("user"))
    env.load("broken", component("broken", "v1"))
    env.reload()

    shared = env.write("shared", "value = 'v2'\n")
    broken = env.write("broken", "import shared\nraise RuntimeError('broken failed')\n")
    result = env.reload(shared, broken)

    assert result.settled == []
    assert [abort.modules for abort in result.aborted] == [["broken", "shared", "user"]]
    assert (env.answer("user"), env.answer("broken")) == ("v1", "v1")
