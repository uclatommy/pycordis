"""Plugin and service composition from revertible effects.

pycordis composes plugins and services from revertible effects and declared
dependencies. A component declares what it needs before it loads, and disposing a
component reverts the effects it registered.

Start with `Context`: create a root context with a scheduler (`ImmediateScheduler` or
`FramePumpScheduler`), then call `Context.use` to run a component in a `Fiber`.
A `Service` and a `Key` give a component access to the services of other components.

Attributes:
    __version__: The version of the installed distribution `pycordis2`, which
        provides the `pycordis` package.
"""

import importlib.metadata

from pycordis.context import Context
from pycordis.deferred import Deferred
from pycordis.errors import CordisError, Issue, ValidationError
from pycordis.fiber import Disposer, EffectHandle, Fiber, FiberState
from pycordis.keys import Key, Keyable
from pycordis.registry import Component, inject
from pycordis.scheduler import FramePumpScheduler, ImmediateScheduler, Scheduler
from pycordis.service import Service

# The import needs an installed distribution (`uv sync` installs it). There is no
# fallback: pyproject.toml holds the placeholder 0.0.0, so a fallback would report a
# false version. The distribution name differs from the import name.
__version__ = importlib.metadata.version("pycordis2")

__all__ = [
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
]
