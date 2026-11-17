"""Test doubles for the loader: a loader that records its changes, and components."""

from collections.abc import Callable
from typing import Any

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.fiber import Fiber
from pycordis.loader import EntryChange, Group, Loader, LoaderConfig

GROUP = "pycordis.loader:Group"


class Mock:
    """A component that records the config of each call, and runs `impl`."""

    def __init__(
        self, name: str, impl: Callable[[Context, Any], Any] | None = None
    ) -> None:
        self.name = name
        self.calls: list[Any] = []
        self._impl = impl

    def __call__(self, ctx: Context, config: Any) -> Any:
        self.calls.append(config)
        return None if self._impl is None else self._impl(ctx, config)

    def reset(self) -> None:
        """Forget the recorded calls."""
        self.calls.clear()


class RecordingLoader(Loader):
    """A loader that keeps its data and records each change.

    Its import function reads a table of components. The name of the group component
    resolves to `Group`, and any other name that is not in the table raises
    `ModuleNotFoundError`. A test sets `gate` to hold an import open: the function
    returns the `Deferred` that `gate(name)` returns.
    """

    def __init__(self, ctx: Context, config: LoaderConfig | None = None) -> None:
        self.data: list[Any] = []
        self.changes: list[EntryChange] = []
        self.modules: dict[str, object] = {}
        self.gate: Callable[[str], Deferred[None] | None] | None = None
        super().__init__(ctx, config, import_function=self._import)

    def commit(self, change: EntryChange) -> None:
        self.data = self.root.data
        self.changes.append(change)

    def _import(self, name: str) -> object | Deferred[object]:
        gate = None if self.gate is None else self.gate(name)
        if gate is None:
            return self._lookup(name)
        return self._after(gate, name)

    def _lookup(self, name: str) -> object:
        if name == GROUP:
            return Group
        if name not in self.modules:
            raise ModuleNotFoundError(f"cannot find module {name!r}", name=name)
        return self.modules[name]

    def _after(self, gate: Deferred[None], name: str) -> Deferred[object]:
        result = Deferred[object]()

        def done(_: Deferred[None]) -> None:
            try:
                result.resolve(self._lookup(name))
            except Exception as error:
                result.reject(error)

        gate.add_done_callback(done)
        return result

    def mock(
        self, name: str, impl: Callable[[Context, Any], Any] | None = None
    ) -> Mock:
        """Add a component to the table, and return it."""
        component = self.modules[name] = Mock(name, impl)
        return component

    def fiber(self, identifier: str) -> Fiber:
        """Return the live fiber of the entry `identifier`."""
        fiber = self.resolve(identifier).live_fiber()
        assert fiber is not None, f"entry {identifier} has no live fiber"
        return fiber

    def running(self, component: object) -> int:
        """Return the number of live fibers of `component`."""
        runtime = self.ctx.registry.get(component)  # type: ignore[arg-type]
        return 0 if runtime is None else len(runtime.fibers)
