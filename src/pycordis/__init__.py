"""Plugin and service composition from revertible effects.

pycordis composes plugins and services from revertible effects and declared
dependencies. A component declares what it needs before it loads, and disposing a
component reverts the effects it registered.

Attributes:
    __version__: The installed distribution version of pycordis.
"""

import importlib.metadata

__version__ = importlib.metadata.version(__name__)

__all__ = ["__version__"]
