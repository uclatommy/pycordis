# pycordis

[![PyPI](https://img.shields.io/pypi/v/pycordis2)](https://pypi.org/project/pycordis2/)
[![Python](https://img.shields.io/pypi/pyversions/pycordis2)](https://pypi.org/project/pycordis2/)
[![CI](https://github.com/uclatommy/pycordis/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/uclatommy/pycordis/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/uclatommy/pycordis/branch/master/graph/badge.svg)](https://codecov.io/gh/uclatommy/pycordis)

pycordis composes plugins and services from **revertible effects** and **declared
dependencies**. Each plugin runs in a fiber. Every side effect that the fiber makes has an
inverse. The framework loads, unloads, and reloads fibers when the services that they depend on
change.

It is a Python implementation of the programming model in *A Programming Paradigm for
Spatiotemporal Composability* (arXiv `2608.25512`). The core package of
[Cordis](https://github.com/cordiverse/cordis) is its reference implementation.

- **Documentation**: <https://uclatommy.github.io/pycordis/>
- **Status**: pre-1.0 (`0.x`). A breaking change to the public API increments the minor version.

## Requirements

- Python 3.12 or later
- No runtime dependencies

## Install

```bash
pip install pycordis2
```

> **Note**: The PyPI name is `pycordis2`. The GitHub repository and the import name are
> `pycordis`. Do not install `pycordis` from PyPI: it is a different project.

## Usage

An effect makes a change and returns the function that reverts it. Disposing the fiber runs the
revert functions.

```python
from pycordis import Context, ImmediateScheduler

root = Context(ImmediateScheduler())


def greeter(ctx: Context, config: object) -> None:
    print("start")
    ctx.effect(lambda: lambda: print("stop"))


fiber = root.use(greeter)  # prints "start"
fiber.dispose()  # prints "stop"
```

A plugin that injects a service waits until a provider exists. It unloads when the provider
goes away.

```python
from pycordis import Context, ImmediateScheduler, Service, inject


class Database(Service):
    provide = "db"

    def cordis_init(self):
        print("database open")
        return lambda: print("database closed")


@inject("db")
class Report:
    def __init__(self, ctx: Context, config: object) -> None:
        print("report loads")


root = Context(ImmediateScheduler())
report = root.use(Report)  # waits: nothing provides "db"
database = root.use(Database)  # prints "database open", then "report loads"
database.dispose()  # the report unloads first, then "database closed"
```

The [User Guide](https://uclatommy.github.io/pycordis/user_guide/) explains fibers, effects,
services, and schedulers.

## Contributing

See [Getting Started](https://uclatommy.github.io/pycordis/getting_started/) for the
development setup.

- **Tracked upstream revision**: `cordiverse/cordis` `68c9d9917871d8680bc7ccb9034b9284d5102433`
  (`v4.0.0-rc.13`)
- **Semantic contract and conformance**:
  [Architecture](https://uclatommy.github.io/pycordis/architecture/#semantic-contract)
- **Divergences from upstream**:
  [Architecture](https://uclatommy.github.io/pycordis/architecture/#divergences-from-upstream)
- **Porting workflow**:
  [Developer Guide](https://uclatommy.github.io/pycordis/developer_guide/#port-an-upstream-change)

## License

MIT. pycordis is a derivative work of Cordis, Copyright (c) 2021-present Shigma. See
[LICENSE](https://github.com/uclatommy/pycordis/blob/master/LICENSE).
