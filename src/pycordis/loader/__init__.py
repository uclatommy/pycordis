"""Load plugins from data.

The loader keeps a tree of plugin entries (a component name, a config, and options) and
reconciles the fiber tree with it. A host whose plugin set comes from data gives the
data, and the loader makes the `use`, `dispose`, and `update` calls. The loader does not
read, parse, or write files: it takes lists and dicts, and a host that saves changes
overrides `EntryTree.commit`.

Start with `Loader`, and give it a list of entry options (`EntryOptions`) with
`Loader.read`. Name an entry `"pycordis.loader:Group"` for a list of child entries.
`EntryTree` is the base class of a component that holds a nested tree.

This module is not imported by `pycordis`: import it when you need it.
"""

from pycordis.loader._entry import Entry, EntryOptions, EntryValidationError
from pycordis.loader._group import EntryGroup, Group
from pycordis.loader._service import Loader, LoaderConfig
from pycordis.loader._tree import EntryChange, EntryTree, ImportFunction

__all__ = [
    "Entry",
    "EntryChange",
    "EntryGroup",
    "EntryOptions",
    "EntryTree",
    "EntryValidationError",
    "Group",
    "ImportFunction",
    "Loader",
    "LoaderConfig",
]
