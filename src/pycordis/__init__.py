"""Plugin and service composition from revertible effects.

pycordis composes plugins and services from revertible effects and declared
dependencies. A component declares what it needs before it loads, and disposing a
component reverts the effects it registered.

Start with `Context`: create a root context with a scheduler (`ImmediateScheduler` or
`FramePumpScheduler`), then call `Context.use` to run a component in a `Fiber`.
A `Service` and a `Key` give a component access to the services of other components.

Attributes:
    __version__: The installed distribution version of pycordis.
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

__version__ = importlib.metadata.version(__name__)

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
