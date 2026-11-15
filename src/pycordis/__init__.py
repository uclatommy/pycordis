"""Plugin and service composition from revertible effects, ported from Cordis.

pycordis is a Python port of the core package of
[Cordis](https://github.com/cordiverse/cordis) (`packages/core`). The upstream revision
that the port tracks is in the repository `README.md`.

Attributes:
    __version__: The installed distribution version of pycordis.
"""

import importlib.metadata

__version__ = importlib.metadata.version(__name__)

__all__ = ["__version__"]
