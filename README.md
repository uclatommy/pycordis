# pycordis

[![CI](https://github.com/uclatommy/pycordis/actions/workflows/ci.yml/badge.svg?branch=master)](https://github.com/uclatommy/pycordis/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/uclatommy/pycordis/branch/master/graph/badge.svg)](https://codecov.io/gh/uclatommy/pycordis)

pycordis is a Python implementation of the programming model in *A Programming Paradigm for
Spatiotemporal Composability* (arXiv `2608.25512`), with the core package of
[Cordis](https://github.com/cordiverse/cordis) (`packages/core`) as its reference
implementation. It composes plugins and services from **revertible effects** and **declared
dependencies**: each plugin runs in a fiber, every side effect that the fiber makes has an
inverse, and the framework loads, unloads, and reloads fibers when the services that they depend
on change.

**Documentation**: <https://uclatommy.github.io/pycordis/>
**Status**: in development, no release yet. The work plan is in
[docs/build_specs/](docs/build_specs/index.md).

## Upstream

- **Tracked upstream revision**: `cordiverse/cordis` `68c9d9917871d8680bc7ccb9034b9284d5102433`
  (`v4.0.0-rc.13`)
- **Semantic contract and conformance**: [docs/architecture.md](docs/architecture.md#semantic-contract)
- **Divergences from upstream**: [docs/architecture.md](docs/architecture.md#divergences-from-upstream)
- **Porting workflow**: [docs/developer_guide.md](docs/developer_guide.md#port-an-upstream-change)

## Requirements

- Python 3.12 or later
- No runtime dependencies

## Install

pycordis is not on a package index. Install it from a git tag:

```bash
uv add "pycordis @ git+https://github.com/uclatommy/pycordis@vX.Y.Z"
```

See [Getting Started](docs/getting_started.md) for development setup.

## License

MIT. pycordis is a derivative work of Cordis, Copyright (c) 2021-present Shigma. See
[LICENSE](LICENSE).
