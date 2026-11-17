"""Reload changed modules and move the live fibers of their components to the new code.

A development tool or an interactive host that knows which files changed can ask this
package to find the loaded modules that the change affects, import them again, and
replace the components that they export.

Start with `Hmr`: `Hmr.reconcile` takes the changed paths and returns a `ReloadResult`.
`source_graph` gives the default `ModuleGraph`, the function that lists the modules that
a loaded module imports.

This module is not imported by `pycordis`: import it when you need it.
"""

from pycordis.hmr._graph import ModuleGraph, source_graph
from pycordis.hmr._service import (
    Abort,
    Hmr,
    HmrConfig,
    ReloadResult,
    Skip,
    SkipReason,
)

__all__ = [
    "Abort",
    "Hmr",
    "HmrConfig",
    "ReloadResult",
    "Skip",
    "SkipReason",
    "ModuleGraph",
    "source_graph",
]
