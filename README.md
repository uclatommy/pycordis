# pycordis

[![PyPI](https://img.shields.io/pypi/v/pycordis2)](https://pypi.org/project/pycordis2/)
[![Python](https://img.shields.io/pypi/pyversions/pycordis2)](https://pypi.org/project/pycordis2/)
[![CI](https://github.com/uclatommy/pycordis/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/uclatommy/pycordis/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/uclatommy/pycordis/branch/master/graph/badge.svg)](https://codecov.io/gh/uclatommy/pycordis)

Sooner or later, a Python app that runs for a long time needs plugins: a bot that loads
commands, a desktop tool with add-ons, a simulation you change while it runs. Loading a plugin
is easy. Unloading it is where things go wrong: an event handler that now fires twice, a timer
that keeps ticking, a plugin that still holds the database connection you just closed. Most
apps give up and restart the process.

**pycordis makes every plugin the perfect houseguest: they don't arrive until their host is
home, and when they leave, they put everything back where they found it. The host
can also decide which rooms a guest may enter, and show them the other guests' rooms 
through a window instead of handing them a key.** A plugin declares the services it needs, 
and pycordis loads it only when they exist. When a service changes or goes away, pycordis 
undoes everything the plugin did, then loads the plugin again when it can. You write the setup 
code once, and pycordis handles teardown, reloads, and ordering. It has no dependencies, 
doesn't use `asyncio`, and runs inside your own event loop or frame loop.

## See It Work

A `Database` service opens a connection and returns the function that closes it. A `Dashboard`
plugin needs the database and registers an event listener. The comments show the output.

```python
from pycordis import Context, ImmediateScheduler, Service, inject


class Database(Service):
    provide = "db"

    def __init__(self, ctx: Context, config: dict) -> None:
        super().__init__(ctx)
        self.url = config["url"]

    def cordis_init(self):
        print(f"open {self.url}")
        return lambda: print(f"close {self.url}")  # how to undo the open


@inject("db")
class Dashboard:
    def __init__(self, ctx: Context, config: object) -> None:
        db = ctx["db"]
        print(f"dashboard loads with {db.url}")
        ctx.on("refresh", lambda: print(f"query {db.url}"))


root = Context(ImmediateScheduler())
dashboard = root.use(Dashboard)  # waits: nothing provides "db" yet
db = root.use(Database, {"url": "sqlite:///dev.db"})
# open sqlite:///dev.db
# dashboard loads with sqlite:///dev.db

db.update({"url": "postgres://prod"})  # change the service
# close sqlite:///dev.db
# open postgres://prod
# dashboard loads with postgres://prod

root.emit("refresh")
# query postgres://prod     <- one listener, not two

db.dispose()  # remove the service
# close postgres://prod     (the dashboard waits again)
```

The dashboard has no cleanup code. pycordis removed its listener before each reload, and closed
each connection after the dashboard unloaded.

## What You Get

- **Automatic cleanup**: every listener, service, and effect that a plugin registers is undone
  in reverse order when the plugin unloads
- **Dependency-driven lifecycle**: a plugin loads when its services exist, and reloads when they
  change
- **Failure isolation**: a plugin that raises fails alone, and its partial setup is undone
- **Your loop, your thread**: `ImmediateScheduler` for scripts and tests, `FramePumpScheduler`
  for a GUI or game frame loop. No `asyncio`, no runtime dependencies
- **Plugins from data**: `pycordis.loader` loads and updates a plugin tree from a list of
  entries
- **Hot reload**: `pycordis.hmr` imports changed modules again and moves the live plugins to
  the new code, without a restart
- **Timers**: `pycordis.timer` gives timeouts and intervals that stop when their plugin unloads

The [User Guide](https://uclatommy.github.io/pycordis/user_guide/) explains fibers, effects,
services, and schedulers.

## About

pycordis is a Python implementation of the programming model in *A Programming Paradigm for
Spatiotemporal Composability* (arXiv `2608.25512`). The core package of
[Cordis](https://github.com/cordiverse/cordis) is its reference implementation.

pycordis differs from Cordis in one area: an experimental `restrict` function limits which
services a plugin can use or provide, and gives that plugin read-only views of other plugins'
fibers and contexts. pycordis trusts every plugin that it loads, so `restrict` stops mistakes
and undeclared access, but it is not a sandbox against hostile code.

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
